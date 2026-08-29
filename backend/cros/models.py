from datetime import datetime, timezone
from typing import Annotated, Any, List, Optional

from bson import ObjectId
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from .constants import Priority


def _to_str_id(v: Any) -> Any:
    if isinstance(v, ObjectId):
        return str(v)
    return v


PyObjectId = Annotated[str, BeforeValidator(_to_str_id)]


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class BaseDocument(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: Optional[PyObjectId] = Field(default=None, alias="_id")

    def to_mongo(self) -> dict:
        d = self.model_dump(by_alias=True, exclude_none=True)
        d.pop("_id", None)
        return d

    @classmethod
    def from_mongo(cls, doc: dict | None):
        if doc is None:
            return None
        return cls.model_validate(doc)


class GeoPoint(BaseModel):
    type: str = "Point"
    coordinates: List[float]  # [lon, lat]


class EventEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    event_id: str
    event_type: str
    schema_version: int = 1
    origin_device_id: str
    origin_actor_id: Optional[str] = None
    logical_timestamp: str
    wall_clock_timestamp: str
    causal_parent_ids: List[str] = Field(default_factory=list)
    priority: Priority = Priority.NORMAL
    ttl_seconds: int = 86400
    correlation_id: str
    signature: Optional[str] = None
    payload: dict = Field(default_factory=dict)


class ErrorDetail(BaseModel):
    field: str
    issue: str
