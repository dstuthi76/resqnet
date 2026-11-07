"""Packet signatures for mesh SOS messages.

Each phone has an Ed25519 key pair. It signs a canonical string of the SOS
fields, so relaying phones cannot alter a message without detection. The
backend trusts the first public key it sees for a device id (trust on first
use) and rejects later packets for that device signed with another key.

Canonical string (the Flutter app builds exactly the same):
    id|lat|lng|ts|people|needs|text|device_id
  lat/lng: 6 decimals, or empty if missing
  people: integer or empty
  needs: comma-joined, sorted
"""
import base64
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

import db


def canonical(p):
    def f(v):
        return "" if v is None else f"{float(v):.6f}"
    people = p.get("people")
    needs = ",".join(sorted(p.get("needs") or []))
    return "|".join([
        str(p.get("id", "")), f(p.get("lat")), f(p.get("lng")), str(p.get("ts", "")),
        "" if people in (None, "") else str(int(people)), needs, p.get("text") or "", str(p.get("device_id", "")),
    ])


def verify(p):
    """Return 'valid', 'unsigned', 'invalid' or 'key_mismatch'."""
    sig, pk = p.get("sig"), p.get("pk")
    if not sig or not pk:
        return "unsigned"
    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(pk))
        key.verify(base64.b64decode(sig), canonical(p).encode("utf-8"))
    except (InvalidSignature, ValueError, TypeError):
        return "invalid"
    dev = p.get("device_id")
    if dev:
        known = db.one("SELECT pubkey FROM devices WHERE device_id=?", (dev,))
        if known is None:
            db.x("INSERT OR IGNORE INTO devices (device_id, pubkey, first_seen) VALUES (?,?,?)",
                 (dev, pk, time.time()))
        elif known["pubkey"] != pk:
            return "key_mismatch"
    return "valid"
