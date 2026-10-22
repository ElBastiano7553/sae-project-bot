"""Choix du fournisseur d'IA d'après les réglages."""

from __future__ import annotations

from src.ai.base import AIProvider

PROVIDERS = ("none", "anthropic", "fake")


def build_provider(provider: str, api_key: str = "", model: str | None = None) -> AIProvider | None:
    """Retourne le fournisseur configuré, ou ``None`` si l'IA est désactivée.

    La validation (fournisseur connu, clé présente) est faite dans ``config.Settings``.
    """
    if provider == "anthropic":
        from src.ai.anthropic_provider import AnthropicProvider

        return AnthropicProvider(api_key, model)
    if provider == "fake":
        from src.ai.fake import FakeProvider

        return FakeProvider(model=model or "fake-model")
    return None
