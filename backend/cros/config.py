import os
from pathlib import Path
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")

MONGO_URL = os.environ["MONGO_URL"]
DB_NAME = os.environ["DB_NAME"]
SIM_DB_NAME = f"{DB_NAME}_simulation"
JWT_SECRET = os.environ["JWT_SECRET"]
JWT_ALGORITHM = "HS256"
CORS_ORIGINS = os.environ.get("CORS_ORIGINS", "*")
EMERGENT_LLM_KEY = os.environ.get("EMERGENT_LLM_KEY")
AI_MODEL_PROVIDER = os.environ.get("AI_MODEL_PROVIDER", "anthropic")
AI_MODEL_NAME = os.environ.get("AI_MODEL_NAME", "claude-sonnet-4-6")
ADMIN_EMAIL = os.environ["ADMIN_EMAIL"]
ADMIN_PASSWORD = os.environ["ADMIN_PASSWORD"]
SEED_PASSWORD = os.environ["SEED_PASSWORD"]
EDGE_KEYSTORE_DIR = ROOT_DIR / ".edge_keystore"
API_V1 = "/api/v1"
SUPPORTED_SCHEMA_VERSIONS = [1]
