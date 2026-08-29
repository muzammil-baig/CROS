import base64
import json
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

SIGNED_FIELDS = [
    "event_id",
    "event_type",
    "schema_version",
    "origin_device_id",
    "origin_actor_id",
    "logical_timestamp",
    "wall_clock_timestamp",
    "causal_parent_ids",
    "priority",
    "ttl_seconds",
    "correlation_id",
    "payload",
]


def canonical_bytes(envelope: dict) -> bytes:
    subset = {k: envelope.get(k) for k in SIGNED_FIELDS}
    return json.dumps(subset, sort_keys=True, separators=(",", ":"), default=str).encode()


def generate_keypair():
    """Returns (private_key_b64_raw, public_key_b64_spki). Private key is NEVER persisted server-side."""
    priv = Ed25519PrivateKey.generate()
    priv_raw = priv.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_spki = priv.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return base64.b64encode(priv_raw).decode(), base64.b64encode(pub_spki).decode()


def sign(private_key_b64: str, envelope: dict) -> str:
    priv = Ed25519PrivateKey.from_private_bytes(base64.b64decode(private_key_b64))
    return base64.b64encode(priv.sign(canonical_bytes(envelope))).decode()


def _load_public(public_key_b64: str) -> Ed25519PublicKey:
    raw = base64.b64decode(public_key_b64)
    if len(raw) == 32:
        return Ed25519PublicKey.from_public_bytes(raw)
    key = serialization.load_der_public_key(raw)
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("unsupported key type")
    return key


def verify(public_key_b64: str, envelope: dict, signature_b64: str) -> bool:
    try:
        pub = _load_public(public_key_b64)
        pub.verify(base64.b64decode(signature_b64), canonical_bytes(envelope))
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False
