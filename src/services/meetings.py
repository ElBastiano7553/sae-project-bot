"""Gestionnaire de réunions (Samae 2.0, lot 1), sans dépendance à discord.py.

Règles appliquées ici, pour chaque commande et chaque bouton :

* **contrôle** (démarrer, étapes, terminer, annuler, inviter, décisions) : le
  responsable de la réunion ou un administrateur du bot ;
* **contribution** (point à l'ordre du jour, réserve, désaccord, action) : les
  participants, le responsable et les administrateurs ;
* **consultation** : tous les membres du serveur.

Cycle de vie : ``planned`` → ``running`` → ``finished`` ; ``planned`` ou
``running`` → ``cancelled``. Une réunion terminée ou annulée n'est plus modifiable,
son historique et son compte rendu (figé à la clôture) sont conservés.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import time
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import asdict, dataclass

from src.database.meetings import MeetingDetail, MeetingRepository, RevisionConflict
from src.services.design import escape_markdown as _md

PLANNED, RUNNING, FINISHED, CANCELLED = "planned", "running", "finished", "cancelled"
OPEN_STATUSES = (PLANNED, RUNNING)
STATUS_LABELS = {PLANNED: "prévue", RUNNING: "en cours", FINISHED: "terminée", CANCELLED: "annulée"}

DECISION, RESERVE, DISAGREEMENT = "decision", "reserve", "disagreement"
NOTE_LABELS = {DECISION: "décision", RESERVE: "réserve", DISAGREEMENT: "désaccord"}

# Limites : adaptées à Discord (25 options, 1024 caractères par champ) et à une équipe de cinq.
TITLE_MAX = 100
TEXT_MAX = 1000
STEP_TITLE_MAX = 100
NOTE_MAX = 500
RATIONALE_MAX = 300
ACTION_MAX = 300
MAX_STEPS = 25
MAX_PARTICIPANTS = 25
MAX_NOTES = 200
MAX_ACTIONS = 50
MIN_MINUTES, MAX_MINUTES = 5, 600
# Créations par membre et par heure (vérifié en base : tient après un redémarrage).
CREATIONS_PER_HOUR = 5
# Modifications par membre : au plus 30 par minute (protège contre les clics en rafale et les scripts).
CHANGES_PER_MINUTE = 30

_MENTION = re.compile(r"<@!?(\d{15,22})>")


class MeetingError(Exception):
    """Refus expliqué à l'utilisateur (le message ne contient jamais de détail interne)."""

    title = "Action impossible"


class PermissionDenied(MeetingError):
    title = "Accès refusé"


class StalePanel(MeetingError):
    """Le panneau utilisé ne correspond plus à l'état de la réunion."""

    title = "Panneau périmé"


class RateLimited(MeetingError):
    title = "Trop de demandes"


@dataclass(frozen=True)
class Actor:
    """Auteur d'une action, tel que vérifié par la couche Discord."""

    user_id: int
    name: str
    is_admin: bool = False

    @property
    def ref(self) -> tuple[int, str]:
        return (self.user_id, self.name)


class RateLimiter:
    """Fenêtre glissante en mémoire : au plus ``max_calls`` par ``period`` secondes et par clé."""

    def __init__(self, max_calls: int, period: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.max_calls = max_calls
        self.period = period
        self._clock = clock
        self._calls: dict[int, deque[float]] = defaultdict(deque)

    def hit(self, key: int) -> float | None:
        """Enregistre un appel ; retourne le délai d'attente en secondes s'il est refusé."""
        now = self._clock()
        calls = self._calls[key]
        while calls and now - calls[0] >= self.period:
            calls.popleft()
        if len(calls) >= self.max_calls:
            return self.period - (now - calls[0])
        calls.append(now)
        return None


def parse_mentions(text: str | None) -> list[int]:
    """Identifiants des membres mentionnés (``<@123>``), sans doublon, dans l'ordre."""
    seen: list[int] = []
    for match in _MENTION.finditer(text or ""):
        user_id = int(match.group(1))
        if user_id not in seen:
            seen.append(user_id)
    return seen


def parse_steps(text: str | None) -> list[str]:
    """Ordre du jour saisi en une ligne : étapes séparées par « ; » ou « | »."""
    parts = re.split(r"[;|\n]", text or "")
    return [p.strip() for p in parts if p.strip()]


def parse_due_date(text: str | None, today: dt.date | None = None) -> str | None:
    """Échéance « AAAA-MM-JJ » ou « JJ/MM/AAAA » (ou « JJ/MM » : année en cours) → ISO."""
    if not text or not text.strip():
        return None
    value = text.strip()
    today = today or dt.date.today()
    for pattern in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return dt.datetime.strptime(value, pattern).date().isoformat()
        except ValueError:
            continue
    try:
        return dt.datetime.strptime(f"{value}/{today.year}", "%d/%m/%Y").date().isoformat()
    except ValueError:
        raise MeetingError(f"Échéance illisible : « {value[:30]} ». Utilisez par exemple 2026-11-05 ou 05/11/2026.") from None


def _clean(text: str | None, limit: int, label: str, *, required: bool = False, single_line: bool = True) -> str:
    """Texte saisi par un membre : espaces normalisés, longueur vérifiée (refus plutôt que coupure)."""
    value = " ".join((text or "").split()) if single_line else (text or "").strip()
    if required and not value:
        raise MeetingError(f"{label} est obligatoire.")
    if len(value) > limit:
        raise MeetingError(f"{label} dépasse {limit} caractères ({len(value)}).")
    return value


# --- Compte rendu --------------------------------------------------------------


@dataclass(frozen=True)
class ReportSection:
    title: str
    lines: list[str]


@dataclass(frozen=True)
class MeetingReport:
    """Compte rendu prêt à afficher : textes des membres échappés (Markdown neutralisé), noms sous
    forme de mentions Discord (qui ne notifient personne : mentions désactivées pour tout le bot)."""

    meeting_id: int
    title: str
    status: str
    date: str  # « AAAA-MM-JJ HH:MM:SS » UTC (début, sinon création)
    sections: list[ReportSection]
    final: bool  # figé à la clôture, ou aperçu provisoire

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> MeetingReport:
        data = json.loads(raw)
        data["sections"] = [ReportSection(**s) for s in data["sections"]]
        return cls(**data)


def _person(user_id: int) -> str:
    return f"<@{int(user_id)}>"


def _minutes_between(start: str | None, end: str | None) -> int | None:
    try:
        a = dt.datetime.fromisoformat(start or "")
        b = dt.datetime.fromisoformat(end or "")
    except ValueError:
        return None
    return max(int((b - a).total_seconds() // 60), 0)


def build_report(detail: MeetingDetail, *, final: bool = False) -> MeetingReport:
    """Compte rendu calculé uniquement à partir de ce qui a été enregistré."""
    m = detail.meeting
    general = [
        f"Statut : {STATUS_LABELS[m.status]}",
        f"Responsable : {_person(m.leader_id)}",
    ]
    duration = _minutes_between(m.started_at, m.ended_at)
    if duration is not None:
        planned = f" (prévue : {m.planned_minutes} min)" if m.planned_minutes else ""
        general.append(f"Durée effective : {duration} min{planned}")
    elif m.planned_minutes:
        general.append(f"Durée prévue : {m.planned_minutes} min")
    if m.description:
        general.append(f"Description : {_md(m.description)}")

    step_icons = {"done": "✅", "current": "▶️", "pending": "⏭️" if m.status in (FINISHED, CANCELLED) else "▫️"}
    step_suffix = {"done": "", "current": " (en cours)", "pending": " (non traitée)" if m.status in (FINISHED, CANCELLED) else ""}
    steps = [f"{step_icons[s.status]} {s.position}. {_md(s.title)}{step_suffix[s.status]}" for s in detail.steps]
    done = sum(1 for s in detail.steps if s.status == "done")

    def note_line(note) -> str:
        where = f" · étape {note.step_position}" if note.step_position else ""
        line = f"{_md(note.content)} ({_person(note.author_id)}{where})"
        if note.rationale:
            line += f" · motif : {_md(note.rationale)}"
        return line

    decisions = [note_line(n) for n in detail.notes if n.kind == DECISION]
    reserves = [
        f"{'⚠️ Réserve' if n.kind == RESERVE else '⚔️ Désaccord'} : {note_line(n)}" for n in detail.notes if n.kind != DECISION
    ]
    actions = [
        f"{'✅' if a.status == 'done' else '☐'} {_md(a.description)} → {_person(a.assignee_id)}"
        + (f" · échéance {a.due_date}" if a.due_date else "")
        for a in detail.actions
    ]
    sections = [
        ReportSection("Informations", general),
        ReportSection(f"Participants ({len(detail.participants)})", [_person(p.user_id) for p in detail.participants]),
        ReportSection("Objectifs", [_md(m.objectives)] if m.objectives else []),
        ReportSection(f"Étapes traitées ({done}/{len(detail.steps)})", steps),
        ReportSection(f"Décisions adoptées ({len(decisions)})", decisions),
        ReportSection(f"Réserves et désaccords ({len(reserves)})", reserves),
        ReportSection(f"Actions à effectuer ({len(actions)})", actions),
    ]
    return MeetingReport(m.id, m.title, m.status, m.started_at or m.created_at, sections, final)


# --- Service ---------------------------------------------------------------------


class MeetingService:
    def __init__(
        self,
        repo: MeetingRepository,
        *,
        limiter: RateLimiter | None = None,
        now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
    ) -> None:
        self.repo = repo
        self.limiter = limiter or RateLimiter(CHANGES_PER_MINUTE, 60)
        self._now = now

    # --- Lecture ---------------------------------------------------------------

    def get(self, meeting_id: int) -> MeetingDetail:
        detail = self.repo.detail(meeting_id)
        if detail is None:
            raise MeetingError(f"La réunion #{meeting_id} n'existe pas.")
        return detail

    def resolve(self, meeting_id: int | None, actor: Actor, channel_id: int | None) -> MeetingDetail:
        """Réunion désignée, ou à défaut l'unique réunion ouverte de ce salon ou du membre."""
        if meeting_id is not None:
            return self.get(meeting_id)
        candidates = self.repo.open_for(actor.user_id, channel_id)
        if not candidates:
            raise MeetingError("Aucune réunion prévue ou en cours ici : précisez l'option `reunion` (voir `/reunion liste`).")
        in_channel = [m for m in candidates if m.channel_id == channel_id]
        pool = in_channel or candidates
        if len(pool) > 1:
            names = ", ".join(f"#{m.id} {m.title}" for m in pool[:5])
            raise MeetingError(f"Plusieurs réunions possibles ({names}) : précisez l'option `reunion`.")
        return self.get(pool[0].id)

    def report(self, meeting_id: int) -> MeetingReport:
        detail = self.get(meeting_id)
        if detail.meeting.report:
            try:
                return MeetingReport.from_json(detail.meeting.report)
            except (TypeError, ValueError, KeyError):
                pass  # compte rendu figé illisible : on le recalcule à partir des données
        return build_report(detail, final=detail.meeting.status in (FINISHED, CANCELLED))

    # --- Contrôles communs --------------------------------------------------------

    def _throttle(self, actor: Actor) -> None:
        wait = self.limiter.hit(actor.user_id)
        if wait is not None:
            raise RateLimited(f"Trop d'actions en peu de temps : réessayez dans {int(wait) + 1} s.")

    @staticmethod
    def can_control(detail: MeetingDetail, actor: Actor) -> bool:
        return actor.is_admin or actor.user_id == detail.meeting.leader_id

    @classmethod
    def can_contribute(cls, detail: MeetingDetail, actor: Actor) -> bool:
        return cls.can_control(detail, actor) or detail.is_participant(actor.user_id)

    def _require_control(self, detail: MeetingDetail, actor: Actor, what: str) -> None:
        if not self.can_control(detail, actor):
            raise PermissionDenied(
                f"Seul le responsable de la réunion ({_person(detail.meeting.leader_id)}) ou un administrateur du bot peut {what}."
            )

    def ensure_control(self, detail: MeetingDetail, actor: Actor, what: str, statuses: tuple[str, ...] = OPEN_STATUSES) -> None:
        """Vérification préalable (avant une confirmation) ; l'action elle-même revérifie tout."""
        self._require_control(detail, actor, what)
        self._require_status(detail, statuses, what)

    def _require_contributor(self, detail: MeetingDetail, actor: Actor, what: str) -> None:
        if not self.can_contribute(detail, actor):
            raise PermissionDenied(f"Rejoignez d'abord la réunion #{detail.meeting.id} pour {what}.")

    @staticmethod
    def _require_status(detail: MeetingDetail, allowed: tuple[str, ...], what: str) -> None:
        status = detail.meeting.status
        if status not in allowed:
            raise MeetingError(f"Impossible de {what} : la réunion #{detail.meeting.id} est {STATUS_LABELS[status]}.")

    def _load(self, meeting_id: int, expected_revision: int | None = None) -> MeetingDetail:
        """Réunion à modifier ; un bouton d'un panneau périmé est refusé avant toute autre vérification."""
        detail = self.get(meeting_id)
        if expected_revision is not None and expected_revision != detail.meeting.revision:
            raise StalePanel("La réunion a changé depuis l'affichage de ce panneau : il vient d'être actualisé, réessayez.")
        return detail

    def _run(self, detail: MeetingDetail, actor: Actor, action: str, text: str, body) -> MeetingDetail:
        """Applique ``body`` dans une transaction, à condition que la réunion n'ait pas changé depuis sa lecture."""
        try:
            with self.repo.change(detail.meeting.id, actor.ref, action, text, expected_revision=detail.meeting.revision) as change:
                body(change)
        except RevisionConflict:
            raise StalePanel("La réunion vient d'être modifiée par quelqu'un d'autre : réessayez.") from None
        return self.get(detail.meeting.id)

    # --- Création ---------------------------------------------------------------

    def create(
        self,
        actor: Actor,
        *,
        title: str,
        description: str = "",
        objectives: str = "",
        leader: tuple[int, str] | None = None,
        participants: list[tuple[int, str]] | None = None,
        steps: list[str] | None = None,
        planned_minutes: int | None = None,
        channel_id: int | None = None,
    ) -> MeetingDetail:
        self._throttle(actor)
        since = (self._now() - dt.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        if not actor.is_admin and self.repo.count_created_since(actor.user_id, since) >= CREATIONS_PER_HOUR:
            raise RateLimited(f"Vous avez créé {CREATIONS_PER_HOUR} réunions dans l'heure : réessayez plus tard.")
        title = _clean(title, TITLE_MAX, "Le titre", required=True)
        description = _clean(description, TEXT_MAX, "La description", single_line=False)
        objectives = _clean(objectives, TEXT_MAX, "Les objectifs", single_line=False)
        steps = [_clean(s, STEP_TITLE_MAX, "Chaque étape", required=True) for s in (steps or [])]
        if len(steps) > MAX_STEPS:
            raise MeetingError(f"L'ordre du jour est limité à {MAX_STEPS} étapes ({len(steps)} reçues).")
        if planned_minutes is not None and not MIN_MINUTES <= planned_minutes <= MAX_MINUTES:
            raise MeetingError(f"La durée prévue doit être comprise entre {MIN_MINUTES} et {MAX_MINUTES} minutes.")
        leader = leader or actor.ref
        others: list[tuple[int, str]] = []
        for user_id, name in participants or []:
            if user_id != leader[0] and all(user_id != o[0] for o in others):
                others.append((user_id, name))
        if len(others) + 1 > MAX_PARTICIPANTS:
            raise MeetingError(f"Une réunion compte au plus {MAX_PARTICIPANTS} participants.")
        meeting_id = self.repo.create(
            title=title,
            description=description,
            objectives=objectives,
            leader=leader,
            participants=others,
            steps=steps,
            planned_minutes=planned_minutes,
            channel_id=channel_id,
            created_by=actor.ref,
        )
        return self.get(meeting_id)

    # --- Participants --------------------------------------------------------------

    def join(self, meeting_id: int, actor: Actor) -> MeetingDetail:
        self._throttle(actor)
        detail = self.get(meeting_id)
        self._require_status(detail, OPEN_STATUSES, "la rejoindre")
        if detail.is_participant(actor.user_id):
            raise MeetingError(f"Vous participez déjà à la réunion #{meeting_id}.")
        if len(detail.participants) >= MAX_PARTICIPANTS:
            raise MeetingError(f"La réunion #{meeting_id} a déjà {MAX_PARTICIPANTS} participants.")
        return self._run(detail, actor, "arrivée", "", lambda c: c.add_participant(*actor.ref))

    def leave(self, meeting_id: int, actor: Actor) -> MeetingDetail:
        self._throttle(actor)
        detail = self.get(meeting_id)
        self._require_status(detail, OPEN_STATUSES, "la quitter")
        if actor.user_id == detail.meeting.leader_id:
            raise MeetingError("Le responsable ne peut pas quitter sa réunion : annulez-la ou terminez-la.")
        if not detail.is_participant(actor.user_id):
            raise MeetingError(f"Vous ne participez pas à la réunion #{meeting_id}.")
        return self._run(detail, actor, "départ", "", lambda c: c.remove_participant(actor.user_id))

    def invite(self, meeting_id: int, actor: Actor, member: tuple[int, str]) -> MeetingDetail:
        self._throttle(actor)
        detail = self.get(meeting_id)
        self._require_control(detail, actor, "inviter des participants")
        self._require_status(detail, OPEN_STATUSES, "inviter")
        if detail.is_participant(member[0]):
            raise MeetingError(f"{_person(member[0])} participe déjà à la réunion.")
        if len(detail.participants) >= MAX_PARTICIPANTS:
            raise MeetingError(f"La réunion a déjà {MAX_PARTICIPANTS} participants.")
        return self._run(detail, actor, "invitation", str(member[0]), lambda c: c.add_participant(*member))

    # --- Déroulement --------------------------------------------------------------

    def start(self, meeting_id: int, actor: Actor, *, expected_revision: int | None = None) -> MeetingDetail:
        self._throttle(actor)
        detail = self._load(meeting_id, expected_revision)
        self._require_control(detail, actor, "démarrer la réunion")
        self._require_status(detail, (PLANNED,), "la démarrer")
        first = next((s for s in detail.steps if s.status == "pending"), None)

        def body(change):
            change.set_status(RUNNING, started=True)
            if first is not None:
                change.set_step_status(first.position, "current")

        return self._run(detail, actor, "démarrage", "", body)

    def next_step(self, meeting_id: int, actor: Actor, *, expected_revision: int | None = None) -> MeetingDetail:
        self._throttle(actor)
        detail = self._load(meeting_id, expected_revision)
        self._require_control(detail, actor, "changer d'étape")
        self._require_status(detail, (RUNNING,), "changer d'étape")
        current = detail.current_step
        after = [s for s in detail.steps if s.status == "pending" and (current is None or s.position > current.position)]
        if current is None and not after:
            raise MeetingError("Toutes les étapes sont traitées : ajoutez un point (`/reunion ajouter point`) ou terminez la réunion.")

        def body(change):
            if current is not None:
                change.set_step_status(current.position, "done")
            if after:
                change.set_step_status(after[0].position, "current")

        label = after[0].title if after else "ordre du jour terminé"
        return self._run(detail, actor, "étape suivante", label, body)

    def previous_step(self, meeting_id: int, actor: Actor, *, expected_revision: int | None = None) -> MeetingDetail:
        self._throttle(actor)
        detail = self._load(meeting_id, expected_revision)
        self._require_control(detail, actor, "changer d'étape")
        self._require_status(detail, (RUNNING,), "changer d'étape")
        current = detail.current_step
        before = [s for s in detail.steps if s.status == "done" and (current is None or s.position < current.position)]
        if not before:
            raise MeetingError("Il n'y a pas d'étape précédente.")
        target = before[-1]

        def body(change):
            if current is not None:
                change.set_step_status(current.position, "pending")
            change.set_step_status(target.position, "current")

        return self._run(detail, actor, "étape précédente", target.title, body)

    def add_step(self, meeting_id: int, actor: Actor, title: str) -> MeetingDetail:
        self._throttle(actor)
        detail = self.get(meeting_id)
        self._require_contributor(detail, actor, "compléter l'ordre du jour")
        self._require_status(detail, OPEN_STATUSES, "compléter l'ordre du jour")
        title = _clean(title, STEP_TITLE_MAX, "Le point", required=True)
        if len(detail.steps) >= MAX_STEPS:
            raise MeetingError(f"L'ordre du jour est limité à {MAX_STEPS} étapes.")
        return self._run(detail, actor, "point ajouté", title, lambda c: c.add_step(title, actor.user_id))

    def add_note(self, meeting_id: int, actor: Actor, kind: str, content: str, rationale: str | None = None) -> MeetingDetail:
        self._throttle(actor)
        if kind not in NOTE_LABELS:
            raise MeetingError("Type de note inconnu.")
        detail = self.get(meeting_id)
        if kind == DECISION:
            self._require_control(detail, actor, "enregistrer une décision")
        else:
            self._require_contributor(detail, actor, f"enregistrer une {NOTE_LABELS[kind]}")
        self._require_status(detail, (RUNNING,), f"enregistrer une {NOTE_LABELS[kind]}")
        content = _clean(content, NOTE_MAX, "Le texte", required=True)
        rationale = _clean(rationale, RATIONALE_MAX, "Le motif") or None
        if len(detail.notes) >= MAX_NOTES:
            raise MeetingError(f"La réunion a atteint {MAX_NOTES} notes.")
        step = detail.current_step.position if detail.current_step else None
        return self._run(
            detail, actor, NOTE_LABELS[kind], content,
            lambda c: c.add_note(kind, content, rationale, step, actor.user_id, actor.name),
        )

    def add_action(
        self, meeting_id: int, actor: Actor, description: str, assignee: tuple[int, str], due_date: str | None = None
    ) -> MeetingDetail:
        self._throttle(actor)
        detail = self.get(meeting_id)
        self._require_contributor(detail, actor, "ajouter une action")
        self._require_status(detail, (RUNNING,), "ajouter une action")
        description = _clean(description, ACTION_MAX, "L'action", required=True)
        due = parse_due_date(due_date, self._now().date())
        if len(detail.actions) >= MAX_ACTIONS:
            raise MeetingError(f"La réunion a atteint {MAX_ACTIONS} actions.")
        return self._run(
            detail, actor, "action", description,
            lambda c: c.add_action(description, assignee[0], assignee[1], due, actor.user_id),
        )

    # --- Clôture -----------------------------------------------------------------

    def finish(self, meeting_id: int, actor: Actor, *, expected_revision: int | None = None) -> MeetingDetail:
        """Termine la réunion et fige son compte rendu (dans la même transaction)."""
        self._throttle(actor)
        detail = self._load(meeting_id, expected_revision)
        self._require_control(detail, actor, "terminer la réunion")
        self._require_status(detail, (RUNNING,), "la terminer")
        return self._close(detail, actor, FINISHED, "clôture")

    def cancel(self, meeting_id: int, actor: Actor, *, expected_revision: int | None = None) -> MeetingDetail:
        self._throttle(actor)
        detail = self._load(meeting_id, expected_revision)
        self._require_control(detail, actor, "annuler la réunion")
        self._require_status(detail, OPEN_STATUSES, "l'annuler")
        return self._close(detail, actor, CANCELLED, "annulation")

    def _close(self, detail: MeetingDetail, actor: Actor, status: str, action: str) -> MeetingDetail:
        current = detail.current_step

        def body(change):
            if current is not None and status == FINISHED:
                change.set_step_status(current.position, "done")
            change.set_status(status, ended=True)
            # Lecture dans la transaction en cours : le compte rendu figé décrit exactement l'état clôturé.
            closed = self.repo.detail(detail.meeting.id)
            change.set_report(build_report(closed, final=True).to_json())

        return self._run(detail, actor, action, "", body)

    # --- Panneau et compte rendu publiés -------------------------------------------

    def set_panel(self, meeting_id: int, channel_id: int, message_id: int) -> None:
        self.repo.set_panel(meeting_id, channel_id, message_id)

    def set_report_messages(self, meeting_id: int, channel_id: int, message_ids: list[int]) -> None:
        self.repo.set_report_messages(meeting_id, channel_id, message_ids)
