"""Faux fournisseur : réponses fixes, aucun appel réseau, aucune clé.

Utilisé par les tests, et utilisable avec ``AI_PROVIDER=fake`` pour essayer les
commandes sur Discord sans clé d'API : chaque réponse est clairement marquée
comme réponse de test.
"""

from __future__ import annotations

from src.ai.base import AIError, AIResponse

FAKE_PREFIX = "[Réponse de test : AI_PROVIDER=fake, aucune IA n'a été appelée]"


class FakeProvider:
    name = "fake"

    def __init__(self, replies: list[str] | None = None, *, error: AIError | None = None, model: str = "fake-model") -> None:
        self.model = model
        self.replies = list(replies or [])
        self.error = error
        self.calls: list[tuple[str, str, int]] = []

    async def complete(self, system: str, prompt: str, *, max_tokens: int) -> AIResponse:
        self.calls.append((system, prompt, max_tokens))
        if self.error is not None:
            raise self.error
        text = self.replies.pop(0) if self.replies else f"{FAKE_PREFIX}\nDemande reçue ({len(prompt)} caractères de contexte)."
        return AIResponse(text=text, model=self.model, input_tokens=len(prompt) // 4, output_tokens=len(text) // 4)

    async def close(self) -> None:
        return None
