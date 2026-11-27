"""Assistant de projet (/samae) : intentions reconnues sans IA, actions jamais appliquées sans validation, IA en option."""

from __future__ import annotations

import pytest

from src.ai.fake import FakeProvider
from src.database import Database
from src.database.ideas import DebateRepository, IdeaRepository, TierListRepository
from src.database.meetings import MeetingRepository
from src.database.repositories import AIRequestRepository
from src.services.ai_assistant import AIAssistant
from src.services.ideas import IdeaService
from src.services.meetings import Actor, MeetingService, RateLimiter
from src.services.project_assistant import ProjectAssistant, detect_intent, extract_idea
from src.services.tracker import TrackerService
from tests.tracker_fixture import build_workbook

ADMIN = Actor(1, "Romain", True, 42)
BOB = Actor(2, "Bob", False, 42)


@pytest.mark.parametrize(
    ("question", "intent"),
    [
        ("Quelles fonctionnalités restent à développer ?", "remaining"),
        ("Fais-moi le bilan de notre avancement.", "progress"),
        ("Quelles sont les dernières décisions prises ?", "decisions"),
        ("Prépare notre prochaine réunion.", "prepare_meeting"),
        ("Quelles idées sont encore en attente ?", "pending_ideas"),
        ("Résume les discussions de cette semaine.", "week"),
        ("Ajoute cette idée à notre base de données : un boss qui vole", "add_idea"),
        ("Quelles questions restent ouvertes ?", "open_questions"),
        ("cherche inventaire", "search"),
        ("Quel temps fait-il à Paris ?", "free"),
    ],
)
def test_example_questions_are_recognised(question, intent):
    assert detect_intent(question) == intent


def test_extract_idea_name_and_description():
    assert extract_idea("Ajoute cette idée : Boss volant — il change de phase") == ("Boss volant", "il change de phase")
    assert extract_idea("ajoute l'idée « Mode photo »")[0] == "Mode photo"


@pytest.fixture
def env(tmp_path):
    db = Database(tmp_path / "sae_bot.sqlite3")
    meetings = MeetingService(MeetingRepository(db))
    ideas_repo = IdeaRepository(db)
    tracker = TrackerService(db, tmp_path / "tracker")
    provider = FakeProvider(["Réponse libre [1]"])
    ai = AIAssistant(provider, AIRequestRepository(db), daily_limit=10, cooldown_seconds=0)
    assistant = ProjectAssistant(
        meetings=meetings.repo, ideas=ideas_repo, tierlists=TierListRepository(db), debates=DebateRepository(db),
        tracker=tracker, ai=None, passages=lambda q: [],
    )
    yield assistant, tracker, IdeaService(ideas_repo, limiter=RateLimiter(100, 60)), meetings, ai, provider
    db.close()


async def test_answers_without_tracker_explain_how_to_import(env):
    assistant, *_ = env
    reply = await assistant.answer("Quelles fonctionnalités restent à développer ?", BOB)
    assert "/suivi importer" in reply.lines[0] and not reply.used_ai


async def test_remaining_features_and_progress_come_from_the_tracker(env):
    assistant, tracker, *_ = env
    tracker.apply_import(build_workbook(), "LUNASTEH.xlsx", ADMIN)
    remaining = await assistant.answer("Quelles fonctionnalités restent à développer ?", BOB)
    assert remaining.title == "Fonctionnalités restantes (2)" and "Inventaire" in remaining.lines[0]
    progress = await assistant.answer("Fais-moi le bilan de notre avancement", BOB)
    assert "1/3 terminées (33 %)" in progress.lines[0]
    decisions = await assistant.answer("Quelles sont les dernières décisions prises ?", BOB)
    assert any("Pygame" in line for line in decisions.lines)


async def test_pending_ideas_prepare_meeting_and_week(env):
    assistant, tracker, ideas, meetings, *_ = env
    ideas.propose(BOB, name="Coop locale", play_style="Coop", genre="Party game")
    pending = await assistant.answer("Quelles idées sont encore en attente ?", BOB)
    assert any("Coop locale" in line for line in pending.lines)
    meeting = meetings.create(ADMIN, title="Point sprint")
    meetings.repo.adopt_orphans(42)
    agenda = await assistant.answer("Prépare notre prochaine réunion", BOB)
    assert any("Classer les 1 idée(s)" in line for line in agenda.lines)
    assert any("/reunion creer" in line for line in agenda.lines)
    week = await assistant.answer("Résume les discussions de cette semaine", BOB)
    assert any(f"Réunion #{meeting.meeting.id}" in line for line in week.lines)
    assert "ne lit pas les messages" in week.notes[0]


async def test_add_idea_is_only_prepared_never_saved(env):
    assistant, _, ideas, *_ = env
    reply = await assistant.answer("Ajoute cette idée : Boss volant — il change de phase", BOB)
    assert reply.action is not None and reply.action.name == "Boss volant"
    assert ideas.repo.list(42) == []  # rien n'est enregistré sans le formulaire validé


async def test_free_questions_without_ai_list_capabilities(env):
    assistant, *_ = env
    reply = await assistant.answer("Quel temps fait-il ?", BOB)
    assert reply.intent == "free" and not reply.used_ai and any("bilan" in line for line in reply.lines)


async def test_free_questions_use_ai_when_configured(env):
    assistant, tracker, _, _, ai, provider = env
    assistant.ai = ai
    tracker.apply_import(build_workbook(), "LUNASTEH.xlsx", ADMIN)
    reply = await assistant.answer("Pourquoi avons-nous choisi Pygame ?", BOB)
    assert reply.used_ai and reply.lines == ["Réponse libre [1]"]
    system, prompt, _ = provider.calls[0]
    assert "<etat_du_projet>" in prompt and "Pygame" in prompt and "validée par un humain" in system
