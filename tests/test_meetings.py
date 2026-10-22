"""Réunions (Samae 2.0, lot 1) : règles métier, permissions, transactions, reprise après redémarrage."""

from __future__ import annotations

import datetime as dt
import sqlite3

import pytest

from src.database import Database
from src.database.connection import MIGRATIONS
from src.database.meetings import MeetingRepository, RevisionConflict
from src.services.meetings import (
    CANCELLED,
    CREATIONS_PER_HOUR,
    DECISION,
    DISAGREEMENT,
    FINISHED,
    MAX_STEPS,
    PLANNED,
    RESERVE,
    RUNNING,
    Actor,
    MeetingError,
    MeetingReport,
    MeetingService,
    PermissionDenied,
    RateLimited,
    RateLimiter,
    StalePanel,
    build_report,
    parse_due_date,
    parse_mentions,
    parse_steps,
)

LEADER = Actor(1, "Alice")
MEMBER = Actor(2, "Bob")
OUTSIDER = Actor(3, "Chloé")
ADMIN = Actor(9, "Admin", is_admin=True)


@pytest.fixture
def service(db):
    return MeetingService(MeetingRepository(db))


def make_meeting(service, steps=("Bilan", "Démo", "Planning"), **kwargs):
    return service.create(
        LEADER,
        title=kwargs.pop("title", "Point sprint 3"),
        objectives=kwargs.pop("objectives", "Décider du planning"),
        participants=kwargs.pop("participants", [(MEMBER.user_id, MEMBER.name)]),
        steps=list(steps),
        planned_minutes=kwargs.pop("planned_minutes", 45),
        channel_id=kwargs.pop("channel_id", 100),
        **kwargs,
    )


def statuses(detail):
    return [s.status for s in detail.steps]


# --- Création ---------------------------------------------------------------------


def test_create_meeting_with_leader_participants_and_agenda(service):
    detail = make_meeting(service, participants=[(2, "Bob"), (2, "Bob"), (1, "Alice")])
    m = detail.meeting
    assert (m.status, m.leader_id, m.revision, m.planned_minutes, m.channel_id) == (PLANNED, 1, 0, 45, 100)
    assert [p.user_id for p in detail.participants] == [1, 2]  # responsable inclus, sans doublon
    assert [s.title for s in detail.steps] == ["Bilan", "Démo", "Planning"]
    assert statuses(detail) == ["pending"] * 3
    assert service.repo.log(m.id)[0].action == "création"


def test_create_with_another_leader(service):
    detail = service.create(MEMBER, title="Revue", leader=(LEADER.user_id, LEADER.name))
    assert detail.meeting.leader_id == LEADER.user_id
    assert detail.meeting.created_by == MEMBER.user_id


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"title": "  "}, "obligatoire"),
        ({"title": "x" * 101}, "100 caractères"),
        ({"steps": ["s"] * (MAX_STEPS + 1)}, "limité"),
        ({"planned_minutes": 2}, "entre 5 et 600"),
        ({"objectives": "o" * 1001}, "1000 caractères"),
    ],
)
def test_create_rejects_invalid_input(service, kwargs, message):
    with pytest.raises(MeetingError, match=message):
        make_meeting(service, **kwargs)
    assert service.repo.list() == []


def test_parsers():
    assert parse_steps("Bilan ; Démo|Plan ;; ") == ["Bilan", "Démo", "Plan"]
    assert parse_mentions("<@123456789012345678> et <@!123456789012345678> <@987654321098765432>") == [
        123456789012345678,
        987654321098765432,
    ]
    assert parse_mentions("@Bob <@12>") == []  # ni texte libre ni identifiant trop court
    today = dt.date(2026, 10, 22)
    assert parse_due_date("2026-11-05", today) == "2026-11-05"
    assert parse_due_date("05/11/2026", today) == "2026-11-05"
    assert parse_due_date("05/11", today) == "2026-11-05"
    assert parse_due_date("", today) is None
    with pytest.raises(MeetingError, match="illisible"):
        parse_due_date("demain", today)


# --- Déroulement -------------------------------------------------------------------


def test_full_lifecycle(service):
    m = make_meeting(service).meeting
    started = service.start(m.id, LEADER)
    assert started.meeting.status == RUNNING and started.meeting.started_at
    assert statuses(started) == ["current", "pending", "pending"]
    assert statuses(service.next_step(m.id, LEADER)) == ["done", "current", "pending"]
    assert statuses(service.previous_step(m.id, LEADER)) == ["current", "pending", "pending"]
    service.next_step(m.id, LEADER)
    service.next_step(m.id, LEADER)
    last = service.next_step(m.id, LEADER)
    assert statuses(last) == ["done", "done", "done"] and last.current_step is None
    with pytest.raises(MeetingError, match="Toutes les étapes"):
        service.next_step(m.id, LEADER)
    # Un point ajouté pendant la réunion devient l'étape suivante.
    service.add_step(m.id, MEMBER, "Questions diverses")
    assert statuses(service.next_step(m.id, LEADER))[-1] == "current"
    finished = service.finish(m.id, LEADER)
    assert finished.meeting.status == FINISHED and finished.meeting.ended_at
    assert statuses(finished) == ["done"] * 4


def test_previous_step_reopens_last_when_agenda_is_done(service):
    m = make_meeting(service, steps=("A", "B")).meeting
    service.start(m.id, LEADER)
    service.next_step(m.id, LEADER)
    service.next_step(m.id, LEADER)
    assert statuses(service.previous_step(m.id, LEADER)) == ["done", "current"]


def test_meeting_without_agenda(service):
    m = make_meeting(service, steps=()).meeting
    started = service.start(m.id, LEADER)
    assert started.current_step is None
    with pytest.raises(MeetingError, match="Toutes les étapes"):
        service.next_step(m.id, LEADER)
    with pytest.raises(MeetingError, match="pas d'étape précédente"):
        service.previous_step(m.id, LEADER)


def test_invalid_states_are_refused(service):
    m = make_meeting(service).meeting
    with pytest.raises(MeetingError, match="est prévue"):
        service.next_step(m.id, LEADER)
    with pytest.raises(MeetingError, match="est prévue"):
        service.add_note(m.id, LEADER, DECISION, "trop tôt")
    with pytest.raises(MeetingError, match="est prévue"):
        service.finish(m.id, LEADER)
    service.start(m.id, LEADER)
    with pytest.raises(MeetingError, match="est en cours"):
        service.start(m.id, LEADER)
    service.finish(m.id, LEADER)
    for operation in (
        lambda: service.start(m.id, LEADER),
        lambda: service.next_step(m.id, LEADER),
        lambda: service.add_step(m.id, LEADER, "encore"),
        lambda: service.add_note(m.id, MEMBER, RESERVE, "trop tard"),
        lambda: service.add_action(m.id, LEADER, "faire", (2, "Bob")),
        lambda: service.join(m.id, OUTSIDER),
        lambda: service.cancel(m.id, LEADER),
        lambda: service.finish(m.id, LEADER),
    ):
        with pytest.raises(MeetingError, match="terminée"):
            operation()
    with pytest.raises(MeetingError, match="n'existe pas"):
        service.get(999)


def test_cancel_keeps_history(service):
    m = make_meeting(service).meeting
    cancelled = service.cancel(m.id, LEADER)
    assert cancelled.meeting.status == CANCELLED and cancelled.meeting.report
    assert [s.title for s in cancelled.steps] == ["Bilan", "Démo", "Planning"]
    actions = [entry.action for entry in service.repo.log(m.id)]
    assert actions[0] == "annulation" and "création" in actions


# --- Permissions -------------------------------------------------------------------


def test_only_leader_or_admin_controls_the_meeting(service):
    m = make_meeting(service).meeting
    for actor in (MEMBER, OUTSIDER):
        for operation in (service.start, service.cancel):
            with pytest.raises(PermissionDenied, match="responsable"):
                operation(m.id, actor)
    service.start(m.id, ADMIN)  # administrateur du bot : autorisé
    for actor in (MEMBER, OUTSIDER):
        for operation in (service.next_step, service.previous_step, service.finish):
            with pytest.raises(PermissionDenied):
                operation(m.id, actor)
        with pytest.raises(PermissionDenied):
            service.add_note(m.id, actor, DECISION, "je décide")
        with pytest.raises(PermissionDenied):
            service.invite(m.id, actor, (4, "Dan"))
    assert service.get(m.id).current_step.position == 1
    service.next_step(m.id, LEADER)
    service.finish(m.id, ADMIN)


def test_contributions_require_participation(service):
    m = make_meeting(service).meeting
    service.start(m.id, LEADER)
    with pytest.raises(PermissionDenied, match="Rejoignez"):
        service.add_note(m.id, OUTSIDER, RESERVE, "pas d'accord")
    with pytest.raises(PermissionDenied):
        service.add_step(m.id, OUTSIDER, "mon point")
    with pytest.raises(PermissionDenied):
        service.add_action(m.id, OUTSIDER, "faire", (3, "Chloé"))
    service.join(m.id, OUTSIDER)
    detail = service.add_note(m.id, OUTSIDER, DISAGREEMENT, "pas d'accord")
    assert detail.notes[-1].kind == DISAGREEMENT and detail.notes[-1].author_id == OUTSIDER.user_id


def test_join_leave_and_invite(service):
    m = make_meeting(service).meeting
    with pytest.raises(MeetingError, match="déjà"):
        service.join(m.id, MEMBER)
    with pytest.raises(MeetingError, match="responsable ne peut pas quitter"):
        service.leave(m.id, LEADER)
    with pytest.raises(MeetingError, match="ne participez pas"):
        service.leave(m.id, OUTSIDER)
    assert service.join(m.id, OUTSIDER).is_participant(OUTSIDER.user_id)
    assert not service.leave(m.id, OUTSIDER).is_participant(OUTSIDER.user_id)
    assert service.invite(m.id, LEADER, (4, "Dan")).is_participant(4)
    with pytest.raises(MeetingError, match="participe déjà"):
        service.invite(m.id, LEADER, (4, "Dan"))


# --- Décisions, réserves, actions, compte rendu ----------------------------------------


def test_notes_and_actions_are_linked_to_current_step(service):
    m = make_meeting(service).meeting
    service.start(m.id, LEADER)
    service.next_step(m.id, LEADER)
    service.add_note(m.id, LEADER, DECISION, "On garde Pygame", "déjà maîtrisé")
    service.add_note(m.id, MEMBER, RESERVE, "Risque sur les délais")
    detail = service.add_action(m.id, MEMBER, "Écrire le README", (2, "Bob"), "2026-11-05")
    decision, reserve = detail.notes
    assert (decision.kind, decision.rationale, decision.step_position) == (DECISION, "déjà maîtrisé", 2)
    assert (reserve.kind, reserve.author_id) == (RESERVE, 2)
    assert (detail.actions[0].assignee_id, detail.actions[0].due_date) == (2, "2026-11-05")
    with pytest.raises(MeetingError, match="500"):
        service.add_note(m.id, LEADER, DECISION, "x" * 501)
    with pytest.raises(MeetingError, match="inconnu"):
        service.add_note(m.id, LEADER, "vote", "x")


def test_final_report_contains_everything_and_is_frozen(service):
    m = make_meeting(service, title="Revue *alpha*").meeting
    service.start(m.id, LEADER)
    service.add_note(m.id, LEADER, DECISION, "Adopter le **plan B**", "moins risqué")
    service.add_note(m.id, MEMBER, RESERVE, "Le planning est serré")
    service.add_note(m.id, MEMBER, DISAGREEMENT, "Je préfère le plan A")
    service.add_action(m.id, LEADER, "Prévenir le tuteur", (1, "Alice"))
    service.next_step(m.id, LEADER)
    service.finish(m.id, LEADER)

    report = service.report(m.id)
    assert report.final and report.status == FINISHED
    sections = {s.title.split(" (")[0]: s.lines for s in report.sections}
    assert set(sections) == {
        "Informations", "Participants", "Objectifs", "Étapes traitées", "Décisions adoptées",
        "Réserves et désaccords", "Actions à effectuer",
    }
    assert sections["Participants"] == ["<@1>", "<@2>"]
    assert sections["Objectifs"] == ["Décider du planning"]
    assert sections["Étapes traitées"][0].startswith("✅ 1. Bilan")
    assert "(non traitée)" in sections["Étapes traitées"][2]
    assert "Adopter le \\*\\*plan B\\*\\*" in sections["Décisions adoptées"][0]  # Markdown neutralisé
    assert "motif : moins risqué" in sections["Décisions adoptées"][0]
    assert any("Réserve" in line for line in sections["Réserves et désaccords"])
    assert any("Désaccord" in line for line in sections["Réserves et désaccords"])
    assert "Prévenir le tuteur → <@1>" in sections["Actions à effectuer"][0]
    assert any("Durée effective" in line and "prévue : 45 min" in line for line in sections["Informations"])
    # Le compte rendu figé ne change plus, même si les données sont relues plus tard.
    assert MeetingReport.from_json(service.get(m.id).meeting.report) == report


def test_preview_report_before_closing(service):
    m = make_meeting(service).meeting
    report = service.report(m.id)
    assert not report.final
    assert build_report(service.get(m.id)).sections[3].lines[0].startswith("▫️ 1. Bilan")


# --- Transactions et concurrence -----------------------------------------------------------


def test_stale_panel_click_is_refused_without_side_effect(service):
    m = make_meeting(service).meeting
    started = service.start(m.id, LEADER, expected_revision=m.revision)
    revision = started.meeting.revision
    service.next_step(m.id, LEADER, expected_revision=revision)
    # Deuxième clic sur le même panneau (même révision) : refusé, aucune étape sautée.
    with pytest.raises(StalePanel):
        service.next_step(m.id, LEADER, expected_revision=revision)
    with pytest.raises(StalePanel):
        service.start(m.id, LEADER, expected_revision=m.revision)
    assert statuses(service.get(m.id)) == ["done", "current", "pending"]


def test_failed_change_rolls_back_everything(service, db):
    m = make_meeting(service).meeting
    service.start(m.id, LEADER)
    before = service.get(m.id)
    # Deux étapes « en cours » à la fois : refusé par la base, toute la modification est annulée.
    with pytest.raises(sqlite3.IntegrityError):
        with service.repo.change(m.id, LEADER.ref, "test") as change:
            change.add_note(DECISION, "écrite puis annulée", None, 1, 1, "Alice")
            change.set_step_status(2, "current")
    after = service.get(m.id)
    assert after == before
    with pytest.raises(RevisionConflict):
        with service.repo.change(m.id, LEADER.ref, "test", expected_revision=before.meeting.revision - 1):
            pass
    with pytest.raises(RevisionConflict):
        with service.repo.change(12345, LEADER.ref, "test"):
            pass


def test_repeated_join_is_idempotent_in_database(service):
    m = make_meeting(service).meeting
    with service.repo.change(m.id, OUTSIDER.ref, "arrivée") as change:
        assert change.add_participant(3, "Chloé")
        assert not change.add_participant(3, "Chloé")
    assert [p.user_id for p in service.get(m.id).participants].count(3) == 1


# --- Limites de fréquence ----------------------------------------------------------------------


def test_rate_limiter_sliding_window():
    now = [0.0]
    limiter = RateLimiter(2, 60, clock=lambda: now[0])
    assert limiter.hit(1) is None and limiter.hit(1) is None
    assert limiter.hit(1) == pytest.approx(60)
    assert limiter.hit(2) is None  # chaque membre a sa propre limite
    now[0] = 61
    assert limiter.hit(1) is None


def test_creation_limit_per_hour(service):
    for index in range(CREATIONS_PER_HOUR):
        service.create(MEMBER, title=f"Réunion {index}")
    with pytest.raises(RateLimited, match="dans l'heure"):
        service.create(MEMBER, title="Une de trop")
    service.create(ADMIN, title="Admin : pas de limite de création")


def test_changes_are_throttled(db):
    service = MeetingService(MeetingRepository(db), limiter=RateLimiter(3, 60, clock=lambda: 0.0))
    m = service.create(LEADER, title="Réunion").meeting
    service.join(m.id, MEMBER)
    service.leave(m.id, MEMBER)
    service.join(m.id, MEMBER)
    with pytest.raises(RateLimited, match="réessayez"):
        service.leave(m.id, MEMBER)


# --- Réunion par défaut ------------------------------------------------------------------------


def test_resolve_default_meeting(service):
    with pytest.raises(MeetingError, match="Aucune réunion"):
        service.resolve(None, OUTSIDER, 100)
    first = make_meeting(service, channel_id=100).meeting
    assert service.resolve(None, OUTSIDER, 100).meeting.id == first.id  # réunion du salon
    assert service.resolve(None, MEMBER, 555).meeting.id == first.id  # réunion du participant
    make_meeting(service, channel_id=100, title="Autre")
    with pytest.raises(MeetingError, match="Plusieurs réunions"):
        service.resolve(None, OUTSIDER, 100)
    assert service.resolve(first.id, OUTSIDER, 100).meeting.id == first.id


# --- Persistance et migration ------------------------------------------------------------------


def test_meeting_survives_restart(tmp_path):
    path = tmp_path / "bot.sqlite3"
    db = Database(path)
    service = MeetingService(MeetingRepository(db))
    m = make_meeting(service).meeting
    service.start(m.id, LEADER)
    service.add_note(m.id, MEMBER, RESERVE, "avant le redémarrage")
    service.set_panel(m.id, 100, 555)
    revision = service.get(m.id).meeting.revision
    db.close()

    reopened = Database(path)
    service = MeetingService(MeetingRepository(reopened))
    detail = service.get(m.id)
    assert (detail.meeting.status, detail.meeting.revision, detail.meeting.panel_message_id) == (RUNNING, revision, 555)
    assert detail.notes[0].content == "avant le redémarrage"
    # Un bouton du panneau dessiné avant le redémarrage fonctionne toujours.
    assert statuses(service.next_step(m.id, LEADER, expected_revision=revision)) == ["done", "current", "pending"]
    reopened.close()


def test_migration_v7_only_adds_tables(tmp_path):
    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    for version, script in enumerate(MIGRATIONS[:6], start=1):
        conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {version};\nCOMMIT;")
    conn.execute("INSERT INTO kv_store (key, value) VALUES ('setup_confirmed_at', 'hier')")
    conn.commit()
    conn.close()

    db = Database(path)
    tables = {row[0] for row in db.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"meetings", "meeting_participants", "meeting_steps", "meeting_notes", "meeting_actions", "meeting_log"} <= tables
    assert db.conn.execute("SELECT value FROM kv_store WHERE key = 'setup_confirmed_at'").fetchone()[0] == "hier"
    assert db.schema_version == len(MIGRATIONS)
    assert list((tmp_path / "backups").glob("*-avantv*.sqlite3"))  # sauvegarde avant migration
    db.close()


def test_database_rejects_invalid_status(db):
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute(
            "INSERT INTO meetings (title, leader_id, leader_name, status, created_by) VALUES ('x', 1, 'a', 'bidon', 1)"
        )
