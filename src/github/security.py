"""Vérification de la signature des webhooks GitHub (``X-Hub-Signature-256``).

GitHub calcule un HMAC-SHA256 du corps brut de la requête avec le secret du
webhook et l'envoie sous la forme ``sha256=<64 caractères hexadécimaux>``.
Le bot refait le calcul et compare les deux valeurs en temps constant.

Référence : https://docs.github.com/webhooks/using-webhooks/validating-webhook-deliveries
"""

from __future__ import annotations

import hashlib
import hmac
import re

SIGNATURE_HEADER = "X-Hub-Signature-256"
_SIGNATURE = re.compile(r"^sha256=([0-9a-f]{64})$")


def compute_signature(secret: str, body: bytes) -> str:
    """Valeur attendue de l'en-tête ``X-Hub-Signature-256`` pour ce corps (utilisée aussi par les tests)."""
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def signature_matches(secret: str, body: bytes, received: str | None) -> bool:
    """Vrai si ``received`` est la signature HMAC-SHA256 de ``body`` avec ``secret``.

    Un en-tête absent, mal formé (autre algorithme, longueur incorrecte…) ou un
    secret vide donnent toujours ``False``.
    """
    if not secret or received is None:
        return False
    match = _SIGNATURE.match(received.strip().lower())
    if match is None:
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, match.group(1))
