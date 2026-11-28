"""Lecture du Google Sheet de l'équipe, sans clé d'API ni compte Google.

Google propose, pour un classeur partagé « Tous les utilisateurs disposant du lien peuvent
consulter », une copie au format .xlsx à l'adresse
``https://docs.google.com/spreadsheets/d/<identifiant>/export?format=xlsx``. Samae lit cette
copie (valeurs affichées, résultats des formules compris) et ne peut **rien** modifier dans le
Google Sheet : il n'a aucun droit d'écriture.

Un classeur privé renvoie vers la page de connexion de Google : l'erreur l'explique. Seules les
adresses ``docs.google.com`` sont acceptées (jamais une adresse arbitraire fournie par un message).
"""

from __future__ import annotations

import re

import aiohttp

from src.tracker.workbook import MAX_WORKBOOK_BYTES

EXPORT_URL = "https://docs.google.com/spreadsheets/d/{id}/export?format=xlsx"
_ID = re.compile(r"^[A-Za-z0-9_-]{20,100}$")
_URL = re.compile(r"^https://docs\.google\.com/spreadsheets/d/([A-Za-z0-9_-]{20,100})(?:[/?#].*)?$")
TIMEOUT_SECONDS = 20


class GoogleSheetError(Exception):
    """Classeur illisible : message en français, destiné à l'équipe."""


def spreadsheet_id(value: str) -> str:
    """Identifiant d'un Google Sheet, à partir de son adresse ou de l'identifiant lui-même."""
    text = (value or "").strip()
    match = _URL.match(text)
    if match:
        return match.group(1)
    if _ID.match(text):
        return text
    raise GoogleSheetError("Adresse de Google Sheet invalide : attendu https://docs.google.com/spreadsheets/d/… ou l'identifiant seul.")


def export_url(value: str) -> str:
    return EXPORT_URL.format(id=spreadsheet_id(value))


async def download_xlsx(value: str, *, url: str | None = None, timeout: float = TIMEOUT_SECONDS) -> bytes:
    """Copie .xlsx du Google Sheet. ``url`` ne sert qu'aux tests (faux serveur local)."""
    target = url or export_url(value)
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout), headers={"User-Agent": "Samae-bot"}) as session:
            async with session.get(target, allow_redirects=True, max_redirects=5) as response:
                final_host = response.url.host or ""
                if response.status in (401, 403) or final_host.startswith("accounts.google"):
                    raise GoogleSheetError(
                        "Le Google Sheet n'est pas lisible sans compte : dans Google Sheets, « Partager » → « Accès général » → "
                        "« Tous les utilisateurs disposant du lien » (lecteur)."
                    )
                if response.status == 404:
                    raise GoogleSheetError("Google Sheet introuvable : vérifiez l'adresse.")
                if response.status != 200:
                    raise GoogleSheetError(f"Google a répondu HTTP {response.status} : réessayez plus tard.")
                if response.content_length and response.content_length > MAX_WORKBOOK_BYTES:
                    raise GoogleSheetError("Classeur trop gros pour être vérifié.")
                data = await response.content.read(MAX_WORKBOOK_BYTES + 1)
    except (aiohttp.ClientError, TimeoutError) as exc:
        raise GoogleSheetError(f"Google Sheets injoignable ({type(exc).__name__}) : réessayez plus tard.") from exc
    if len(data) > MAX_WORKBOOK_BYTES:
        raise GoogleSheetError("Classeur trop gros pour être vérifié.")
    if not data.startswith(b"PK"):
        # Une page HTML (connexion, consentement) au lieu du fichier : le classeur n'est pas public.
        raise GoogleSheetError(
            "Google n'a pas renvoyé de fichier .xlsx (classeur privé ?). Partagez-le en lecture à « Tous les utilisateurs disposant du lien »."
        )
    return data
