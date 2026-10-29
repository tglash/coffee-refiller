"""Signing keys for every actor, generated fresh in memory at startup.

AP2 V2 signs mandates as SD-JWT chains with ES256 keys. Real deployments keep
the user's key on the user's device (e.g. a passkey); this prototype keeps all
keys in one process, so it *simulates* that trust boundary rather than
enforcing it. Nothing here is a real credential.
"""

from __future__ import annotations

import json
from functools import cache
from typing import Any

from cryptography.hazmat.primitives.asymmetric import ec
from jwcrypto.jwk import JWK


def _new_key(kid: str) -> JWK:
    jwk = json.loads(JWK.from_pyca(ec.generate_private_key(ec.SECP256R1())).export())
    jwk["kid"] = kid
    return JWK(**jwk)


@cache
def key_for(actor: str) -> JWK:
    """Private key for an actor ("user", "agent", "merchant:beanco", "processor")."""
    return _new_key(actor)


def public_jwk(actor: str) -> dict[str, Any]:
    """Public half as a JWK dict, as used in AP2 `cnf` claims."""
    return json.loads(key_for(actor).export_public())


def public_key(actor: str) -> JWK:
    return JWK(**public_jwk(actor))
