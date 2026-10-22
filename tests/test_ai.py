"""IA : faux fournisseur, quotas, masquage des secrets, fournisseur Claude simulé (aucun appel réseau)."""

import datetime as dt
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from src.ai import AIError, AIRefused, AIUnavailable, build_provider
from src.ai.anthropic_provider import DEFAULT_MODEL, AnthropicProvider
from src.ai.fake import FAKE_PREFIX, FakeProvider
from src.database.repositories import AIRequestRepository, EventRecord
from src.gitlab.api import FileDiff, MergeRequestInfo
from src.services.ai_assistant import MASK, AIAssistant, QuotaExceeded, mask_secrets
from src.services.history import ActivityStats
from src.services.knowledge import Passage

NOW = dt.datetime(2026, 10, 22, 12, 0, tzinfo=dt.UTC)


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


def assistant(db, provider=None, **kwargs):
    options = {"daily_limit": 50, "cooldown_seconds": 0, **kwargs}
    return AIAssistant(provider or FakeProvider(["Réponse [1]"]), AIRequestRepository(db), **options)


# --- Masquage --------------------------------------------------------------------


def test_mask_secrets_known_values_and_patterns():
    text = (
        "clé = valeur-secrete-connue-123\n"
        "token = settings.discord_token\n"  # du code ordinaire : rien à masquer
        "+GITLAB_ACCESS_TOKEN=abcdefgh-faux\n"
        'password = "motdepasse-tres-long"\n'
        + "glpat-" + "X" * 24 + "\n"  # faux token construit à l'exécution : aucun motif de secret dans le dépôt
    )
    masked, count = mask_secrets(text, ["valeur-secrete-connue-123"])
    assert count == 4
    assert "valeur-secrete" not in masked and "abcdefgh-faux" not in masked and "motdepasse" not in masked and "glpat-" not in masked
    assert "settings.discord_token" in masked


# --- Quotas et journal -----------------------------------------------------------------


async def test_ask_sends_sources_and_records_usage_without_text(db):
    provider = FakeProvider(["La démo est le 12/11 [1]."])
    helper = assistant(db, provider)
    answer = await helper.ask(7, "Quand est la démo ?", [Passage("knowledge/sujet.md › Démo", "Démo le 12/11.")])
    assert answer.text == "La démo est le 12/11 [1]."
    assert answer.sources == ("[1] knowledge/sujet.md › Démo",)
    system, prompt, _ = provider.calls[0]
    assert "uniquement à partir des extraits" in system and "Démo le 12/11." in prompt
    row = db.conn.execute("SELECT * FROM ai_requests").fetchone()
    assert (row["user_id"], row["command"], row["status"]) == (7, "ask", "ok")
    assert "démo" not in " ".join(str(v) for v in dict(row).values()).lower()  # le texte n'est pas conservé


async def test_ask_without_sources_does_not_call_the_ai(db):
    provider = FakeProvider()
    answer = await assistant(db, provider).ask(7, "Question sans source", [])
    assert provider.calls == [] and "knowledge/" in answer.text


async def test_secrets_are_masked_before_sending(db):
    provider = FakeProvider(["ok"])
    helper = assistant(db, provider, secrets=["mon-token-discord-factice"])
    answer = await helper.ask(7, "question", [Passage("notes", "le token est mon-token-discord-factice")])
    assert "mon-token-discord-factice" not in provider.calls[0][1] and MASK in provider.calls[0][1]
    assert any("masqué" in note for note in answer.notes)


async def test_daily_limit_and_cooldown(db):
    clock = Clock()
    helper = assistant(db, FakeProvider(["a", "b", "c"]), daily_limit=2, cooldown_seconds=30, clock=clock)
    passages = [Passage("notes", "texte")]
    await helper.ask(1, "q", passages)
    with pytest.raises(QuotaExceeded, match="attendre"):
        await helper.ask(1, "q", passages)
    db.conn.execute("UPDATE ai_requests SET at = '2026-10-22 11:00:00'")
    await helper.ask(1, "q", passages)
    with pytest.raises(QuotaExceeded, match="quota"):
        await helper.ask(2, "q", passages)  # 2 demandes aujourd'hui pour toute l'équipe


async def test_provider_error_is_recorded_and_raised(db):
    helper = assistant(db, FakeProvider(error=AIUnavailable("indisponible")))
    with pytest.raises(AIUnavailable):
        await helper.ask(1, "q", [Passage("notes", "texte")])
    assert db.conn.execute("SELECT status FROM ai_requests").fetchone()["status"] == "error"
    assert helper._in_flight == set()


async def test_summary_uses_only_recorded_events(db):
    provider = FakeProvider(["Bilan"])
    record = EventRecord("push", "sent", "push", "main", "📤 2 commits sur main", None, "Alice", None, "2026-10-21 10:00:00",
                         event_at="2026-10-21 10:00:00")
    answer = await assistant(db, provider).summarize(1, [record], ActivityStats(commits=2, pushes=1), "7 derniers jours")
    assert answer.text == "Bilan"
    assert "2 commits sur main" in provider.calls[0][1]
    empty = await assistant(db, FakeProvider()).summarize(1, [], ActivityStats(), "7 derniers jours")
    assert "rien à résumer" in empty.text


async def test_review_truncates_large_diffs(db):
    provider = FakeProvider(["Verdict : prête à fusionner"])
    mr = MergeRequestInfo(12, "Score", "", "Bob", "feature/score", "main", "opened", None)
    diffs = [FileDiff("a.py", "+x\n" * 5000), FileDiff("b.py", "+y\n")]
    answer = await assistant(db, provider).review(1, mr, diffs)
    assert "diff tronqué" in provider.calls[0][1]
    assert any("a.py" in note for note in answer.notes)


# --- Fabrique et faux fournisseur ---------------------------------------------------------


def test_build_provider():
    assert build_provider("none") is None
    assert isinstance(build_provider("fake"), FakeProvider)
    provider = build_provider("anthropic", "cle-factice-0000", None)
    assert isinstance(provider, AnthropicProvider) and provider.model == DEFAULT_MODEL


async def test_fake_provider_is_clearly_marked():
    response = await FakeProvider().complete("s", "p", max_tokens=10)
    assert response.text.startswith(FAKE_PREFIX)


# --- Fournisseur Claude, client simulé ---------------------------------------------------


class FakeMessages:
    def __init__(self, result):
        self.result = result
        self.params = None

    async def create(self, **params):
        self.params = params
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def fake_client(result):
    messages = FakeMessages(result)
    return SimpleNamespace(beta=SimpleNamespace(messages=messages)), messages


def message(stop_reason="end_turn", text="Bonjour"):
    return SimpleNamespace(
        stop_reason=stop_reason,
        stop_details=SimpleNamespace(category="cyber") if stop_reason == "refusal" else None,
        content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
        model="claude-opus-5-5",
        usage=SimpleNamespace(input_tokens=100, output_tokens=20),
    )


async def test_anthropic_request_shape_and_response():
    client, messages = fake_client(message())
    response = await AnthropicProvider("cle", client=client).complete("système", "texte", max_tokens=8000)
    assert (response.text, response.input_tokens, response.output_tokens) == ("Bonjour", 100, 20)
    params = messages.params
    assert params["model"] == "claude-opus-5-5" and params["system"] == "système"
    assert params["messages"] == [{"role": "user", "content": "texte"}]
    assert params["output_config"] == {"effort": "medium"} and "thinking" not in params
    assert params["fallbacks"] == "default" and params["betas"] == ["server-side-fallback-2026-07-01"]


async def test_anthropic_no_fallback_for_other_models():
    client, messages = fake_client(message())
    await AnthropicProvider("cle", "claude-haiku-5-5", client=client).complete("s", "t", max_tokens=100)
    assert "fallbacks" not in messages.params


async def test_anthropic_refusal_and_truncation():
    client, _ = fake_client(message("refusal"))
    with pytest.raises(AIRefused):
        await AnthropicProvider("cle", client=client).complete("s", "t", max_tokens=100)
    client, _ = fake_client(message("max_tokens"))
    assert (await AnthropicProvider("cle", client=client).complete("s", "t", max_tokens=100)).truncated


def _status_error(cls, status):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("erreur", response=httpx2.Response(status, request=request), body=None)


@pytest.mark.parametrize(
    "error, expected, message_part",
    [
        (_status_error(anthropic.AuthenticationError, 401), AIError, "AI_API_KEY"),
        (_status_error(anthropic.NotFoundError, 404), AIError, "AI_MODEL"),
        (_status_error(anthropic.RateLimitError, 429), AIUnavailable, "trop de demandes"),
        (_status_error(anthropic.InternalServerError, 500), AIUnavailable, "indisponible"),
    ],
)
async def test_anthropic_errors_are_translated(error, expected, message_part):
    client, _ = fake_client(error)
    with pytest.raises(expected, match=message_part):
        await AnthropicProvider("cle", client=client).complete("s", "t", max_tokens=100)


async def test_anthropic_connection_error():
    client, _ = fake_client(anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com")))
    with pytest.raises(AIUnavailable, match="injoignable"):
        await AnthropicProvider("cle", client=client).complete("s", "t", max_tokens=100)
