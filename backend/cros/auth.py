import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import JWT_ALGORITHM, JWT_SECRET
from .constants import Role
from .db import db
from .errors import ApiError, forbidden

# ---------------------------------------------------------------- permissions
P = {
    "request:create": {Role.CITIZEN, Role.FIELD_RESPONDER, Role.INCIDENT_COMMANDER,
                       Role.COMMUNICATIONS_OPERATOR, Role.FIELD_GATEWAY_OPERATOR},
    "request:read": {Role.FIELD_RESPONDER, Role.INCIDENT_COMMANDER, Role.MEDICAL_COORDINATOR,
                     Role.SHELTER_COORDINATOR, Role.COMMUNICATIONS_OPERATOR,
                     Role.GOVERNMENT_OFFICER, Role.ANALYST_PLANNER, Role.FIELD_GATEWAY_OPERATOR},
    "request:prioritize_override": {Role.INCIDENT_COMMANDER},
    "request:cancel": {Role.CITIZEN, Role.INCIDENT_COMMANDER},
    "incident:create": {Role.INCIDENT_COMMANDER, Role.GOVERNMENT_OFFICER},
    "incident:read": {Role.FIELD_RESPONDER, Role.INCIDENT_COMMANDER, Role.MEDICAL_COORDINATOR,
                      Role.SHELTER_COORDINATOR, Role.COMMUNICATIONS_OPERATOR,
                      Role.GOVERNMENT_OFFICER, Role.ANALYST_PLANNER, Role.FIELD_GATEWAY_OPERATOR},
    "incident:update": {Role.INCIDENT_COMMANDER},
    "hazard:create": {Role.CITIZEN, Role.FIELD_RESPONDER, Role.INCIDENT_COMMANDER,
                      Role.ANALYST_PLANNER},
    "hazard:read": {r for r in Role},
    "mission:read": {Role.FIELD_RESPONDER, Role.INCIDENT_COMMANDER, Role.MEDICAL_COORDINATOR,
                     Role.GOVERNMENT_OFFICER, Role.ANALYST_PLANNER, Role.COMMUNICATIONS_OPERATOR},
    "mission:approve": {Role.INCIDENT_COMMANDER},
    "mission:dispatch": {Role.INCIDENT_COMMANDER},
    "mission:respond": {Role.FIELD_RESPONDER},
    "resource:read": {Role.FIELD_RESPONDER, Role.INCIDENT_COMMANDER, Role.MEDICAL_COORDINATOR,
                      Role.SHELTER_COORDINATOR, Role.ANALYST_PLANNER, Role.GOVERNMENT_OFFICER},
    "resource:write": {Role.INCIDENT_COMMANDER, Role.MEDICAL_COORDINATOR},
    "hospital:capacity": {Role.MEDICAL_COORDINATOR, Role.INCIDENT_COMMANDER},
    "shelter:capacity": {Role.SHELTER_COORDINATOR, Role.INCIDENT_COMMANDER},
    "facility:read": {r for r in Role if r != Role.CITIZEN},
    "allocation:propose": {Role.INCIDENT_COMMANDER, Role.ANALYST_PLANNER},
    "recommendation:read": {Role.INCIDENT_COMMANDER, Role.GOVERNMENT_OFFICER,
                            Role.ANALYST_PLANNER, Role.MEDICAL_COORDINATOR},
    "recommendation:decide": {Role.INCIDENT_COMMANDER},
    "recommendation:override": {Role.INCIDENT_COMMANDER},
    "comm:read": {Role.COMMUNICATIONS_OPERATOR, Role.FIELD_GATEWAY_OPERATOR,
                  Role.INCIDENT_COMMANDER, Role.ANALYST_PLANNER},
    "comm:write": {Role.COMMUNICATIONS_OPERATOR, Role.FIELD_GATEWAY_OPERATOR},
    "gateway:admin": {Role.FIELD_GATEWAY_OPERATOR, Role.SYSTEM_ADMINISTRATOR},
    "sync:exchange": {r for r in Role},
    "sync:resolve": {Role.INCIDENT_COMMANDER, Role.COMMUNICATIONS_OPERATOR,
                     Role.FIELD_GATEWAY_OPERATOR},
    "simulation:run": {Role.ANALYST_PLANNER, Role.INCIDENT_COMMANDER, Role.SYSTEM_ADMINISTRATOR},
    "audit:read": {Role.INCIDENT_COMMANDER, Role.GOVERNMENT_OFFICER, Role.ANALYST_PLANNER,
                   Role.SYSTEM_ADMINISTRATOR},
    "user:admin": {Role.SYSTEM_ADMINISTRATOR},
    "org:read": {r for r in Role if r != Role.CITIZEN},
    "device:admin": {Role.SYSTEM_ADMINISTRATOR},
    "device:register": {r for r in Role},
    "observability:read": {Role.SYSTEM_ADMINISTRATOR, Role.ANALYST_PLANNER,
                           Role.INCIDENT_COMMANDER, Role.COMMUNICATIONS_OPERATOR},
    "pii:read": {Role.INCIDENT_COMMANDER, Role.MEDICAL_COORDINATOR, Role.FIELD_RESPONDER},
    "location:precise": {Role.INCIDENT_COMMANDER, Role.FIELD_RESPONDER, Role.MEDICAL_COORDINATOR},
}


def role_permissions(role: str) -> list[str]:
    try:
        r = Role(role)
    except ValueError:
        return []
    return sorted([p for p, roles in P.items() if r in roles])


def has_permission(role: str, permission: str) -> bool:
    try:
        r = Role(role)
    except ValueError:
        return False
    return r in P.get(permission, set())


# ---------------------------------------------------------------- passwords
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except ValueError:
        return False


# ---------------------------------------------------------------- tokens
def create_access_token(user: dict, minutes: int = 720) -> str:
    payload = {
        "sub": user["user_id"],
        "email": user["email"],
        "role": user["role"],
        "org_id": user.get("org_id"),
        "type": "access",
        "exp": datetime.now(timezone.utc) + timedelta(minutes=minutes),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def create_refresh_token(user_id: str) -> str:
    return jwt.encode(
        {"sub": user_id, "type": "refresh",
         "exp": datetime.now(timezone.utc) + timedelta(days=7)},
        JWT_SECRET, algorithm=JWT_ALGORITHM)


def create_offline_permission_token(user: dict, hours: int = 72) -> str:
    """Signed, longer-lived token embedding role+permissions for offline authorization."""
    return jwt.encode(
        {"sub": user["user_id"], "role": user["role"],
         "permissions": role_permissions(user["role"]),
         "type": "offline_permission",
         "exp": datetime.now(timezone.utc) + timedelta(hours=hours)},
        JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise ApiError(401, "TOKEN_EXPIRED", "Token expired")
    except jwt.InvalidTokenError:
        raise ApiError(401, "INVALID_TOKEN", "Invalid token")


bearer = HTTPBearer(auto_error=False)


async def get_current_user(
    request: Request,
    creds: Optional[HTTPAuthorizationCredentials] = Depends(bearer),
) -> dict:
    token = creds.credentials if creds else request.cookies.get("access_token")
    if not token:
        raise ApiError(401, "UNAUTHENTICATED", "Not authenticated")
    payload = decode_token(token)
    if payload.get("type") != "access":
        raise ApiError(401, "INVALID_TOKEN", "Invalid token type")
    user = await db.users.find_one({"user_id": payload["sub"]}, {"_id": 0, "password_hash": 0})
    if not user:
        raise ApiError(401, "UNAUTHENTICATED", "User not found")
    if user.get("disabled"):
        raise ApiError(403, "ACCOUNT_DISABLED", "Account disabled")
    return user


def require(permission: str):
    async def dep(user: dict = Depends(get_current_user)) -> dict:
        if not has_permission(user["role"], permission):
            from .audit import record_unauthorized_attempt
            await record_unauthorized_attempt(user, permission)
            raise forbidden(f"Role '{user['role']}' lacks permission '{permission}'")
        return user
    return dep


# ---------------------------------------------------------------- rate limit
_buckets: dict[str, list[float]] = {}


def rate_limit(key: str, limit: int, window: float = 60.0):
    now = time.time()
    hits = [t for t in _buckets.get(key, []) if now - t < window]
    if len(hits) >= limit:
        raise ApiError(429, "RATE_LIMITED", "Rate limit exceeded")
    hits.append(now)
    _buckets[key] = hits
