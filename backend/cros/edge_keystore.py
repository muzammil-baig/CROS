"""Filesystem-backed device secure-element simulator.

Device private keys are stored on the local filesystem (representing a device
secure element / edge node keystore) and are NEVER written to the application
database. Used by the edge/gateway runtime and by the browser fallback signer
when WebCrypto Ed25519 is unavailable (labelled SIMULATED_SIGNER in the UI).
"""
import json
from typing import Optional

from .config import EDGE_KEYSTORE_DIR
from .crypto import generate_keypair, sign

EDGE_KEYSTORE_DIR.mkdir(mode=0o700, exist_ok=True)


def _path(device_id: str):
    safe = "".join(c for c in device_id if c.isalnum() or c in "-_")
    return EDGE_KEYSTORE_DIR / f"{safe}.json"


def store_private_key(device_id: str, private_key_b64: str):
    p = _path(device_id)
    p.write_text(json.dumps({"device_id": device_id, "private_key": private_key_b64}))
    p.chmod(0o600)


def has_key(device_id: str) -> bool:
    return _path(device_id).exists()


def get_private_key(device_id: str) -> Optional[str]:
    p = _path(device_id)
    if not p.exists():
        return None
    return json.loads(p.read_text())["private_key"]


def provision(device_id: str) -> str:
    """Generate a keypair, keep the private half on the filesystem, return public key."""
    priv, pub = generate_keypair()
    store_private_key(device_id, priv)
    return pub


def sign_envelope(device_id: str, envelope: dict) -> Optional[str]:
    priv = get_private_key(device_id)
    if priv is None:
        return None
    return sign(priv, envelope)
