from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, EmailStr, Field

from .. import edge_keystore
from ..audit import record
from ..auth import (create_access_token, create_offline_permission_token,
                    create_refresh_token, decode_token, get_current_user, rate_limit,
                    role_permissions, verify_password)
from ..config import PERSISTENCE_BACKEND, TEST_MODE
from ..constants import EventType
from ..db import db
from ..errors import ApiError
from ..events import bus
from ..models import utcnow_iso

router = APIRouter(prefix="/auth", tags=["auth"])

MAX_ATTEMPTS = 5
LOCKOUT_MINUTES = 15


class LoginBody(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=200)


class DeviceRegisterBody(BaseModel):
    device_type: str = "web_client"
    public_key: Optional[str] = None
    label: Optional[str] = None


@router.post("/login")
async def login(body: LoginBody, request: Request, response: Response):
    ip = request.client.host if request.client else "unknown"
    email = body.email.lower()
    if not TEST_MODE:
        rate_limit(f"login:{ip}", 20, 60)
    identifier = f"{ip}:{email}"
    att = await db.login_attempts.find_one({"identifier": identifier})
    if att and att.get("count", 0) >= MAX_ATTEMPTS:
        locked_until = datetime.fromisoformat(att["last_at"]) + timedelta(
            minutes=LOCKOUT_MINUTES)
        if datetime.now(timezone.utc) < locked_until:
            raise ApiError(429, "ACCOUNT_LOCKED",
                           f"Too many failed attempts. Try again after {locked_until.isoformat()}")
        await db.login_attempts.delete_one({"identifier": identifier})

    if PERSISTENCE_BACKEND == "postgres":
        from .. import postgres
        user = await postgres.find_user_by_email(email)
    else:
        user = await db.users.find_one({"email": email})
    if not user or not verify_password(body.password, user["password_hash"]):
        await db.login_attempts.update_one(
            {"identifier": identifier},
            {"$inc": {"count": 1}, "$set": {"last_at": datetime.now(timezone.utc).isoformat()}},
            upsert=True)
        await record(actor_id=None, actor_role=None, action="LOGIN_FAILED",
                     entity_type="user", entity_id=email, outcome="denied", immutable=True)
        raise ApiError(401, "INVALID_CREDENTIALS", "Invalid email or password")
    if user.get("disabled"):
        raise ApiError(403, "ACCOUNT_DISABLED", "Account disabled")

    await db.login_attempts.delete_one({"identifier": identifier})
    access = create_access_token(user)
    refresh = create_refresh_token(user["user_id"])
    offline = create_offline_permission_token(user)
    response.set_cookie("access_token", access, httponly=True, secure=True,
                        samesite="none", max_age=43200, path="/")
    response.set_cookie("refresh_token", refresh, httponly=True, secure=True,
                        samesite="none", max_age=604800, path="/")
    await record(actor_id=user["user_id"], actor_role=user["role"], action="LOGIN",
                 entity_type="user", entity_id=user["user_id"])
    return {
        "access_token": access,
        "refresh_token": refresh,
        "offline_permission_token": offline,
        "token_type": "bearer",
        "user": _public_user(user),
        "permissions": role_permissions(user["role"]),
    }


def _public_user(u: dict) -> dict:
    return {"user_id": u["user_id"], "email": u["email"], "name": u.get("name"),
            "role": u["role"], "org_id": u.get("org_id")}


@router.post("/token/refresh")
async def refresh_token(request: Request, response: Response):
    token = request.cookies.get("refresh_token") or \
        request.headers.get("X-Refresh-Token")
    if not token:
        raise ApiError(401, "UNAUTHENTICATED", "Missing refresh token")
    payload = decode_token(token)
    if payload.get("type") != "refresh":
        raise ApiError(401, "INVALID_TOKEN", "Not a refresh token")
    if PERSISTENCE_BACKEND == "postgres":
        from .. import postgres
        user = await postgres.find_user_by_id(payload["sub"])
    else:
        user = await db.users.find_one({"user_id": payload["sub"]})
    if not user:
        raise ApiError(401, "UNAUTHENTICATED", "User not found")
    access = create_access_token(user)
    response.set_cookie("access_token", access, httponly=True, secure=True,
                        samesite="none", max_age=43200, path="/")
    return {"access_token": access, "token_type": "bearer",
            "offline_permission_token": create_offline_permission_token(user)}


@router.post("/logout")
async def logout(response: Response, user: dict = Depends(get_current_user)):
    response.delete_cookie("access_token", path="/")
    response.delete_cookie("refresh_token", path="/")
    await record(actor_id=user["user_id"], actor_role=user["role"], action="LOGOUT",
                 entity_type="user", entity_id=user["user_id"])
    return {"ok": True}


@router.get("/me")
async def me(user: dict = Depends(get_current_user)):
    devices = await db.devices.find({"owner_user_id": user["user_id"]},
                                    {"_id": 0, "device_id": 1, "trust_level": 1,
                                     "revoked": 1, "signing_mode": 1}).to_list(20)
    return {"user": _public_user(user), "permissions": role_permissions(user["role"]),
            "devices": devices}


@router.post("/device/register")
async def register_device(body: DeviceRegisterBody, user: dict = Depends(get_current_user)):
    """Register a device public key.

    If `public_key` is supplied the private key never leaves the client
    (WebCrypto Ed25519). Otherwise a keypair is provisioned in the server-side
    secure-element simulator (filesystem keystore, never the database) and the
    device is marked SIMULATED_SIGNER.
    """
    device_id = f"DEV-WEB-{user['user_id']}" if body.public_key else f"DEV-{user['user_id']}"
    if body.public_key:
        public_key = body.public_key
        signing_mode = "client_webcrypto"
    else:
        public_key = edge_keystore.provision(device_id)
        signing_mode = "server_keystore"
    result = await bus.emit_system(EventType.DEVICE_REGISTERED.value, {
        "device_id": device_id, "public_key": public_key,
        "owner_user_id": user["user_id"], "device_type": body.device_type,
        "signing_mode": signing_mode, "trust_level": "provisional",
        "label": body.label})
    if PERSISTENCE_BACKEND == "postgres":
        from .. import postgres
        await postgres.upsert_device(device_id=device_id, owner_user_id=user["user_id"],
                                     public_key=public_key, signing_mode=signing_mode,
                                     trust_level="provisional")
    return {"device_id": device_id, "signing_mode": signing_mode,
            "simulated_signer": signing_mode == "server_keystore",
            "public_key": public_key, "event_id": result.get("event_id"),
            "registered_at": utcnow_iso()}


@router.get("/credential-status")
async def credential_status(user: dict = Depends(get_current_user)):
    devices = await db.devices.find({"owner_user_id": user["user_id"]},
                                    {"_id": 0}).to_list(20)
    return {
        "user_id": user["user_id"], "role": user["role"],
        "permissions": role_permissions(user["role"]),
        "devices": [{"device_id": d["device_id"], "revoked": bool(d.get("revoked")),
                     "trust_level": d.get("trust_level"),
                     "signing_mode": d.get("signing_mode"),
                     "simulated_signer": d.get("signing_mode") == "server_keystore"}
                    for d in devices],
        "offline_permission_token_supported": True,
        "checked_at": utcnow_iso(),
    }
