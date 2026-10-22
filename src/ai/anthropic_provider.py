"""Fournisseur Claude (Anthropic), via le SDK officiel ``anthropic``.

* Modèle par défaut : ``claude-opus-5-5`` (modifiable avec ``AI_MODEL``).
* Réflexion adaptative (comportement par défaut du modèle) et effort ``medium`` :
  suffisant pour résumer et relire, sans coût inutile.
* Repli côté serveur (``fallbacks: "default"``) pour les modèles qui le prennent en
  charge : si le modèle décline une demande légitime, l'API la confie à un autre
  modèle dans le même appel, au lieu de renvoyer un refus.
* Les erreurs du SDK sont traduites en ``AIError`` lisibles, sans détail interne
  ni clé. Le SDK réessaie déjà lui-même les erreurs temporaires (429, 5xx, réseau).
"""

from __future__ import annotations

import logging

import anthropic

from src.ai.base import AIError, AIRefused, AIResponse, AIUnavailable

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-5-5"
EFFORT = "medium"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Modèles qui acceptent le repli côté serveur sous sa forme « default ».
FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
TIMEOUT_SECONDS = 120.0
MAX_RETRIES = 2


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str, model: str | None = None, *, client: anthropic.AsyncAnthropic | None = None) -> None:
        self.model = model or DEFAULT_MODEL
        self._client = client or anthropic.AsyncAnthropic(api_key=api_key, timeout=TIMEOUT_SECONDS, max_retries=MAX_RETRIES)

    async def complete(self, system: str, prompt: str, *, max_tokens: int) -> AIResponse:
        params: dict = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
            "output_config": {"effort": EFFORT},
        }
        if self.model in FALLBACK_MODELS:
            params["betas"] = [FALLBACK_BETA]
            params["fallbacks"] = "default"
        try:
            response = await self._client.beta.messages.create(**params)
        except anthropic.AuthenticationError as exc:
            log.error("IA : clé AI_API_KEY refusée par Anthropic (requête %s)", _request_id(exc))
            raise AIError("La clé d'API de l'IA est refusée : un administrateur doit vérifier AI_API_KEY.") from exc
        except anthropic.PermissionDeniedError as exc:
            log.error("IA : accès refusé au modèle %s (requête %s)", self.model, _request_id(exc))
            raise AIError("Le compte d'IA n'a pas accès à ce modèle : vérifiez AI_MODEL.") from exc
        except anthropic.NotFoundError as exc:
            log.error("IA : modèle %s introuvable (requête %s)", self.model, _request_id(exc))
            raise AIError("Le modèle d'IA configuré est introuvable : vérifiez AI_MODEL.") from exc
        except anthropic.RateLimitError as exc:
            log.warning("IA : limite de débit atteinte (requête %s)", _request_id(exc))
            raise AIUnavailable("L'IA reçoit trop de demandes pour le moment : réessayez dans une minute.") from exc
        except anthropic.BadRequestError as exc:
            log.error("IA : requête refusée (%s, requête %s)", exc.message, _request_id(exc))
            raise AIError("La demande n'a pas pu être traitée par l'IA (contexte trop long ?).") from exc
        except anthropic.APIStatusError as exc:
            log.warning("IA : erreur %s du service (requête %s)", exc.status_code, _request_id(exc))
            raise AIUnavailable("Le service d'IA est momentanément indisponible : réessayez plus tard.") from exc
        except anthropic.APIConnectionError as exc:  # comprend APITimeoutError
            log.warning("IA : service injoignable (%s)", type(exc).__name__)
            raise AIUnavailable("Le service d'IA est injoignable : réessayez plus tard.") from exc

        if response.stop_reason == "refusal":
            category = getattr(response.stop_details, "category", None) if response.stop_details else None
            log.info("IA : demande déclinée par le modèle (catégorie %s)", category)
            raise AIRefused("L'IA a refusé de répondre à cette demande.")
        text = "\n".join(block.text for block in response.content if block.type == "text").strip()
        if not text:
            raise AIError("L'IA n'a renvoyé aucun texte.")
        return AIResponse(
            text=text,
            model=response.model,
            input_tokens=response.usage.input_tokens or 0,
            output_tokens=response.usage.output_tokens or 0,
            truncated=response.stop_reason == "max_tokens",
        )

    async def close(self) -> None:
        await self._client.close()


def _request_id(exc: anthropic.APIStatusError) -> str:
    return getattr(exc, "request_id", None) or "?"
