import logging
import uuid

from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).parent / ".env")

from fastapi import APIRouter, FastAPI, Request  # noqa: E402
from fastapi.exceptions import RequestValidationError  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from starlette.middleware.cors import CORSMiddleware  # noqa: E402

from cros import projections  # noqa: F401,E402  (registers event consumers)
from cros.config import API_V1, CORS_ORIGINS, SUPPORTED_SCHEMA_VERSIONS  # noqa: E402
from cros.db import db, ensure_indexes  # noqa: E402
from cros.errors import ApiError, error_body  # noqa: E402
from cros.events import bus, ensure_cloud_identity  # noqa: E402
from cros.models import utcnow_iso  # noqa: E402
from cros.observability import incr, span  # noqa: E402
from cros.routers import (admin, auth_router, comm, dashboard, incidents, missions,  # noqa: E402
                          recommendations, requests as requests_router, resources,
                          simulation as simulation_router)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("cros")

app = FastAPI(title="Crisis Response OS (CROS)", version="1.0.0")

v1 = APIRouter(prefix=API_V1)
for r in (auth_router.router, requests_router.router, incidents.router, missions.router,
          resources.router, recommendations.router, comm.router, simulation_router.router,
          admin.router, dashboard.router):
    v1.include_router(r)

legacy = APIRouter(prefix="/api")


@legacy.get("/")
async def root():
    return {"service": "CROS", "api": API_V1,
            "supported_schema_versions": SUPPORTED_SCHEMA_VERSIONS,
            "docs": "/docs"}


@legacy.get("/health")
async def health():
    try:
        await db.command("ping")
        return {"status": "ok", "database": "up", "at": utcnow_iso()}
    except Exception as exc:
        return JSONResponse(status_code=503,
                            content={"status": "degraded", "database": "down",
                                     "error": str(exc)[:200]})


app.include_router(v1)
app.include_router(legacy)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=CORS_ORIGINS.split(","),
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-Id"],
)


@app.middleware("http")
async def observability_middleware(request: Request, call_next):
    request_id = request.headers.get("X-Request-Id") or str(uuid.uuid4())
    request.state.request_id = request_id
    with span("http.request", method=request.method, path=request.url.path,
              request_id=request_id):
        response = await call_next(request)
    response.headers["X-Request-Id"] = request_id
    incr(f"http.{response.status_code // 100}xx")
    return response


@app.exception_handler(ApiError)
async def api_error_handler(request: Request, exc: ApiError):
    rid = getattr(request.state, "request_id", str(uuid.uuid4()))
    return JSONResponse(status_code=exc.status_code,
                        content=error_body(exc.code, exc.message, rid, exc.details))


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    rid = getattr(request.state, "request_id", str(uuid.uuid4()))
    details = [{"field": ".".join(str(p) for p in e.get("loc", [])[1:]),
                "issue": e.get("msg", "invalid")} for e in exc.errors()]
    return JSONResponse(status_code=422,
                        content=error_body("VALIDATION_FAILED",
                                           "Request body validation failed", rid, details))


@app.on_event("startup")
async def startup():
    await ensure_indexes()
    await ensure_cloud_identity()
    from cros.seed import credentials_markdown, ensure_gateway_identities, seed
    result = await seed()
    await ensure_gateway_identities()
    logger.info("seed: %s", result)
    try:
        Path("/app/memory").mkdir(exist_ok=True)
        Path("/app/memory/test_credentials.md").write_text(await credentials_markdown())
    except Exception as exc:
        logger.warning("could not write test credentials: %s", exc)
    logger.info("CROS ready: %d event handlers registered", bus.handler_count())


@app.on_event("shutdown")
async def shutdown():
    from cros.db import client
    client.close()
