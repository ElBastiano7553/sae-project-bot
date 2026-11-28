"""Gestionnaire de réunions (1.14.0) : idées, tier lists S→F à votes masqués, débats et compte rendu."""

from __future__ import annotations

import pytest

from src.database.ideas import DebateRepository, IdeaRepository, TierListRepository
from src.database.meetings import MeetingRepository
from src.services.ideas import (
    MAX_ACTIVE_IDEAS,
    REJECTED,
    RETAINED,
    DebateService,
    IdeaError,
    IdeaService,
    TierListService,
    compute_results,
    meeting_sections,
    similarity,
    tier_for_mean,
)
from src.services.meetings import Actor, MeetingService, PermissionDenied, RateLimiter

GUILD = 42
ADMIN = Actor(1, "Romain", True, GUILD)
BOB = Actor(2, "Bob", False, GUILD)
CHLOE = Actor(3, "Chloé", False, GUILD)
DAN = Actor(4, "Dan", False, GUILD)
OTHER_GUILD = Actor(5, "Eve", True, 99)


@pytest.fixture
def services(db):
    ideas_repo, tl_repo, debates_repo = IdeaRepository(db), TierListRepository(db), DebateRepository(db)
    decided = []
    ideas = IdeaService(ideas_repo, limiter=RateLimiter(1000, 60))
    tierlists = TierListService(tl_repo, ideas_repo)
    debates = DebateService(debates_repo, ideas_repo, tl_repo, on_decided=lambda d, a: decided.append((d.id, a.user_id)))
    return ideas, tierlists, debates, decided


def propose(ideas, actor, name, genre="Plateforme", style="Solo", description=""):
    idea, _ = ideas.propose(actor, name=name, play_style=style, genre=genre, description=description)
    return idea


# --- Idées ------------------------------------------------------------------------------


def test_member_can_propose_at_most_five_active_ideas(services):
    ideas, *_ = services
    created = [propose(ideas, BOB, f"Idée {n}", genre=f"Genre {n}") for n in range(MAX_ACTIVE_IDEAS)]
    with pytest.raises(IdeaError, match="déjà 5 idées"):
        propose(ideas, BOB, "Une de trop")
    # Retirer une idée libère une place ; les autres membres ne sont pas concernés.
    ideas.withdraw(created[0].id, BOB)
    propose(ideas, BOB, "Remplaçante")
    propose(ideas, CHLOE, "Idée de Chloé")


def test_required_fields_and_lengths_are_checked(services):
    ideas, *_ = services
    with pytest.raises(IdeaError, match="style de jeu"):
        ideas.propose(BOB, name="Nom", play_style=" ", genre="RPG")
    with pytest.raises(IdeaError, match="80 caractères"):
        ideas.propose(BOB, name="x" * 81, play_style="Solo", genre="RPG")


def test_similar_ideas_are_detected_and_can_be_grouped(services):
    ideas, *_ = services
    a = propose(ideas, BOB, "Roguelike spatial", genre="Roguelike", style="Solo", description="vaisseau et donjons générés")
    b, similar = ideas.propose(CHLOE, name="Roguelike dans l'espace", play_style="Solo", genre="Roguelike",
                               description="donjons générés à bord d'un vaisseau")
    assert [i.id for i, _ in similar] == [a.id]
    assert similarity(a, b) >= 0.34
    unrelated = propose(ideas, DAN, "Simulateur de ferme", genre="Gestion", style="Détente")
    assert all(i.id != unrelated.id for i, _ in ideas.similar_to(a))
    with pytest.raises(PermissionDenied):
        ideas.group(unrelated.id, b.id, BOB)  # Bob n'est l'auteur d'aucune des deux
    ideas.group(b.id, a.id, CHLOE)
    families = ideas.catalogue(BOB)
    family = next(f for f in families if f[0].id == a.id)
    assert [m.id for m in family[1]] == [b.id]


def test_only_author_or_admin_edits_and_only_admin_sets_status(services):
    ideas, *_ = services
    idea = propose(ideas, BOB, "Coop locale")
    with pytest.raises(PermissionDenied):
        ideas.edit(idea.id, CHLOE, name="Volée")
    assert ideas.edit(idea.id, BOB, genre="Party game").genre == "Party game"
    with pytest.raises(PermissionDenied):
        ideas.set_status(idea.id, BOB, RETAINED)
    assert ideas.set_status(idea.id, ADMIN, RETAINED, "validé en réunion").status == RETAINED
    with pytest.raises(IdeaError, match="en attente"):
        ideas.edit(idea.id, BOB, name="Trop tard")
    history = [h["action"] for h in ideas.repo.history(idea.id)]
    assert history[0] == "statut:retained" and "proposition" in history


def test_ideas_of_another_server_are_invisible(services):
    ideas, *_ = services
    idea = propose(ideas, BOB, "Secrète")
    with pytest.raises(IdeaError, match="n'existe pas"):
        ideas.get(idea.id, OTHER_GUILD)
    assert ideas.catalogue(OTHER_GUILD) == []


def test_search_ignores_accents_and_case(services):
    ideas, *_ = services
    propose(ideas, BOB, "Énigmes temporelles", genre="Puzzle")
    assert [i.name for i in ideas.search(BOB, "enigme")] == ["Énigmes temporelles"]


# --- Calcul des résultats ------------------------------------------------------------------


def test_scale_and_rounding():
    assert tier_for_mean(5.5) == "S" and tier_for_mean(5.49) == "A" and tier_for_mean(1.2) == "F"


def test_results_mean_ties_extremes_and_disagreements():
    items = [(1, "Consensus"), (2, "Clivante"), (3, "Égale"), (4, "Sans vote")]
    votes = {
        1: [(10, "A"), (11, "A"), (12, "A"), (13, "B")],
        2: [(10, "S"), (11, "S"), (12, "S"), (13, "F")],  # un avis extrême, désaccord
        3: [(10, "A"), (11, "A"), (12, "A"), (13, "B")],  # même moyenne que 1 → égalité
    }
    data = compute_results(items, votes)
    by_id = {r["idea_id"]: r for r in data["ideas"]}
    assert by_id[1]["mean"] == 4.75 and by_id[1]["tier"] == "A" and not by_id[1]["disagreement"]
    assert by_id[2]["mean"] == 4.75 and by_id[2]["disagreement"]
    assert by_id[2]["extremes"] == [{"tier": "F", "others_mean": 6.0}]
    assert sorted(map(sorted, data["ties"])) == [[1, 2, 3]]
    assert by_id[1]["rank"] == by_id[3]["rank"] == by_id[2]["rank"] == 1
    assert by_id[4]["mean"] is None and by_id[4]["votes"] == 0
    assert 2 in data["controversial"] and 1 not in data["controversial"]
    # Aucun identifiant de votant dans les résultats publiés.
    assert "10" not in str(data["ideas"]) and "voter" not in str(data)


def test_f_rated_idea_is_flagged_not_eliminated():
    data = compute_results([(1, "Mal aimée")], {1: [(10, "F"), (11, "F"), (12, "D")]})
    assert data["low"] == [1]
    assert data["ideas"][0]["tier"] == "F" and data["ideas"][0]["rank"] == 1


# --- Tier lists ------------------------------------------------------------------------------


def test_tierlist_vote_validate_close_flow(services):
    ideas, tierlists, *_ = services
    a, b = propose(ideas, BOB, "Alpha", genre="A"), propose(ideas, CHLOE, "Beta", genre="B")
    with pytest.raises(PermissionDenied):
        tierlists.create(BOB, "Classement")
    tl = tierlists.create(ADMIN, "Classement")
    assert tierlists.repo.item_ids(tl.id) == [a.id, b.id]

    tierlists.vote(tl.id, BOB, a.id, "S")
    tierlists.vote(tl.id, BOB, a.id, "B")  # changement d'avis avant validation
    with pytest.raises(IdeaError, match="toutes les idées"):
        tierlists.validate(tl.id, BOB)
    tierlists.vote(tl.id, BOB, b.id, "f")
    tierlists.validate(tl.id, BOB)
    with pytest.raises(IdeaError, match="déjà validé"):
        tierlists.vote(tl.id, BOB, a.id, "S")
    tierlists.vote(tl.id, CHLOE, a.id, "A")  # bulletin commencé mais pas validé : non compté

    progress = tierlists.progress(tl.id, ADMIN)
    assert progress == {"validated": [BOB.user_id], "started": [CHLOE.user_id], "items": 2}
    with pytest.raises(IdeaError, match="masqués"):
        tierlists.results(tl.id, BOB)
    with pytest.raises(PermissionDenied):
        tierlists.close(tl.id, BOB)
    closed = tierlists.close(tl.id, ADMIN)
    data = tierlists.results(closed.id, BOB)
    by_id = {r["idea_id"]: r for r in data["ideas"]}
    assert by_id[a.id]["mean"] == 4.0 and by_id[b.id]["tier"] == "F"
    assert data["voters"] == 1 and data["unvalidated"] == 1
    with pytest.raises(IdeaError, match="clôturée"):
        tierlists.vote(tl.id, DAN, a.id, "S")


def test_cannot_vote_for_an_idea_outside_the_tierlist(services):
    ideas, tierlists, *_ = services
    a = propose(ideas, BOB, "Dans la liste")
    tl = tierlists.create(ADMIN, "Liste", idea_ids=[a.id])
    outside = propose(ideas, BOB, "Hors liste", genre="Autre")
    with pytest.raises(IdeaError, match="ne fait pas partie"):
        tierlists.vote(tl.id, BOB, outside.id, "A")
    with pytest.raises(IdeaError, match="Rang inconnu"):
        tierlists.vote(tl.id, BOB, a.id, "Z")


# --- Débats ----------------------------------------------------------------------------------


def test_debate_arguments_tiebreak_and_human_confirmation(services):
    ideas, tierlists, debates, decided = services
    idea = propose(ideas, BOB, "Clivante")
    debate = debates.open(CHLOE, idea_id=idea.id)
    with pytest.raises(IdeaError, match="déjà ouvert"):
        debates.open(BOB, idea_id=idea.id)
    debates.add_entry(debate.id, BOB, "pour", "Original")
    debates.add_entry(debate.id, CHLOE, "objection", "Trop long à produire")
    with pytest.raises(PermissionDenied):
        debates.start_vote(debate.id, BOB, ["Garder", "Abandonner"])
    with pytest.raises(IdeaError, match="2 à 5"):
        debates.start_vote(debate.id, ADMIN, ["Seule"])
    debates.start_vote(debate.id, ADMIN, ["Garder", "Simplifier", "Garder"])  # doublon ignoré
    options = debates.repo.options(debate.id)
    assert [o.label for o in options] == ["Garder", "Simplifier"]
    debates.vote(debate.id, BOB, options[0].id)
    debates.vote(debate.id, CHLOE, options[1].id)
    debates.vote(debate.id, CHLOE, options[0].id)  # vote modifié
    tally = debates.tally(debates.get(debate.id, BOB))
    assert tally["voters"] == 2 and [o.label for o in tally["leaders"]] == ["Garder"] and not tally["tie"]

    proposed = debates.propose(debate.id, BOB, "On garde l'idée, version courte", RETAINED)
    with pytest.raises(PermissionDenied):
        debates.confirm(debate.id, BOB, proposed.proposal)
    # Une proposition modifiée entre l'affichage et le clic n'est pas confirmée par erreur.
    debates.propose(debate.id, CHLOE, "On abandonne", REJECTED)
    with pytest.raises(IdeaError, match="a changé"):
        debates.confirm(debate.id, ADMIN, proposed.proposal)
    final = debates.confirm(debate.id, ADMIN, "On abandonne")
    assert final.status == "decided" and final.decision == "On abandonne" and final.decided_by == ADMIN.user_id
    assert ideas.get(idea.id, BOB).status == REJECTED
    assert decided == [(debate.id, ADMIN.user_id)]
    with pytest.raises(IdeaError):
        debates.add_entry(debate.id, BOB, "pour", "Trop tard")


def test_controversial_ideas_come_from_closed_results(services):
    ideas, tierlists, debates, _ = services
    a, b = propose(ideas, BOB, "Consensuelle", genre="A"), propose(ideas, CHLOE, "Clivante", genre="B")
    tl = tierlists.create(ADMIN, "TL")
    with pytest.raises(IdeaError, match="clôture"):
        debates.controversial(tl.id, BOB)
    for actor, tiers in ((BOB, ("A", "S")), (CHLOE, ("A", "S")), (DAN, ("A", "F"))):
        tierlists.vote(tl.id, actor, a.id, tiers[0])
        tierlists.vote(tl.id, actor, b.id, tiers[1])
        tierlists.validate(tl.id, actor)
    tierlists.close(tl.id, ADMIN)
    assert [r["idea_id"] for r in debates.controversial(tl.id, BOB)] == [b.id]


# --- Compte rendu de réunion ----------------------------------------------------------------


def test_meeting_report_includes_tierlists_ideas_and_debates(db, services):
    ideas, tierlists, debates, _ = services
    meetings = MeetingService(
        MeetingRepository(db),
        extra_sections=lambda d: meeting_sections(d.meeting.id, d.meeting.guild_id, ideas.repo, tierlists.repo, debates.repo),
    )
    meeting_id = meetings.create(ADMIN, title="Choix du concept", steps=["Tier list"]).meeting.id
    meetings.repo.adopt_orphans(GUILD)
    kept = propose(ideas, BOB, "Gardée", genre="A")
    dropped = propose(ideas, CHLOE, "Écartée", genre="B")
    tl = tierlists.create(ADMIN, "Concepts", meeting_id=meeting_id)
    tierlists.vote(tl.id, BOB, kept.id, "S")
    tierlists.vote(tl.id, BOB, dropped.id, "D")
    tierlists.validate(tl.id, BOB)
    tierlists.close(tl.id, ADMIN)
    ideas.set_status(kept.id, ADMIN, RETAINED)
    ideas.set_status(dropped.id, ADMIN, REJECTED)
    debates.open(BOB, title="Moteur 2D ou 3D ?", meeting_id=meeting_id)
    report = meetings.report(meeting_id, GUILD)
    titles = [s.title for s in report.sections]
    assert "Tier lists (1)" in titles and "Idées retenues (1)" in titles and "Idées écartées (1)" in titles
    assert "Questions ouvertes (1)" in titles
    text = str(report.sections)
    assert "Gardée" in text and "Moteur 2D ou 3D" in text


def test_meeting_service_survives_failing_extra_sections(db):
    repo = MeetingRepository(db)
    meetings = MeetingService(repo, extra_sections=lambda d: (_ for _ in ()).throw(RuntimeError("panne")))

    class Detail:
        class meeting:
            id = 1

    assert meetings._extras(Detail()) == []


# --- Migration v15 ---------------------------------------------------------------------------


def test_v15_only_creates_tables():
    from src.database.connection import MIGRATIONS

    script = MIGRATIONS[14].upper()
    assert script.count("CREATE TABLE") == 13
    for forbidden in ("DROP ", "DELETE ", "UPDATE ", "ALTER "):
        assert forbidden not in script


def test_upgrade_from_v14_keeps_data_and_backs_up_first(tmp_path):
    import sqlite3

    from src.database import Database
    from src.database.connection import MIGRATIONS

    path = tmp_path / "v14.sqlite3"
    conn = sqlite3.connect(path)
    for number, script in enumerate(MIGRATIONS[:14], start=1):
        conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {number};\nCOMMIT;")
    conn.execute("INSERT INTO meetings (title, leader_id, leader_name, created_by, guild_id) VALUES ('Réunion', 1, 'A', 1, 42)")
    conn.commit()
    conn.close()
    db = Database(path)
    try:
        assert db.schema_version == len(MIGRATIONS)
        assert db.conn.execute("SELECT title FROM meetings").fetchone()[0] == "Réunion"
        assert list((tmp_path / "backups").glob(f"*-avantv{len(MIGRATIONS)}.sqlite3"))
    finally:
        db.close()
