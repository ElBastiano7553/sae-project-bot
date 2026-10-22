"""Interface commune des fournisseurs d'IA."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class AIResponse:
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    truncated: bool = False  # réponse coupée par la limite de longueur


class AIError(Exception):
    """Échec d'un appel à l'IA. Le message est montré à l'utilisateur : jamais de détail interne."""


class AIUnavailable(AIError):
    """Service momentanément indisponible (réseau, surcharge, quota) : réessayer plus tard."""


class AIRefused(AIError):
    """Le modèle a refusé de répondre à cette demande."""


class AIProvider(Protocol):
    name: str
    model: str

    async def complete(self, system: str, prompt: str, *, max_tokens: int) -> AIResponse:
        """Envoie une demande (consignes + texte) et retourne la réponse. Lève ``AIError``."""

    async def close(self) -> None:
        """Libère les connexions."""
