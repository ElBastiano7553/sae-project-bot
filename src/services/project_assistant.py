"""Assistant de projet de Samae (1.14.0) : « /samae demande:… » et, en option, la mention @Samae.

Fonctionnement en deux niveaux, pour que l'essentiel marche **sans IA payante** :

1. **Questions reconnues** (bilan, fonctionnalités restantes, décisions, idées en attente, questions
   ouvertes, préparation de réunion, résumé de la semaine, recherche, ajout d'une idée) : réponse
   calculée directement à partir des données de Samae (réunions, idées, tier lists, débats, suivi Excel).
   Gratuit, instantané, toujours exact.
2. **Autres questions** : si une IA est configurée (``AI_PROVIDER``), elle répond à partir d'un instantané
   du projet et des documents de l'équipe, qu'elle doit citer ; sinon Samae explique ce qu'il sait faire.

Les consultations sont automatiques. Une **modification** (ajouter une idée…) n'est jamais faite
directement : l'assistant prépare l'action et la personne la confirme par un bouton (formulaire prérempli).
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field

from src.database.ideas import DebateRepository, IdeaRepository, TierListRepository
from src.database.meetings import MeetingRepository
from src.services.design import escape_markdown as _md
from src.services.ideas import ACTIVE, DEBATE_STATUS_LABELS, IDEA_STATUS_LABELS
from src.services.meetings import Actor
from src.services.tracker import DECISIONS, FEATURES, IDEAS, OPEN_QUESTIONS, TrackerError, TrackerService

MAX_LINES = 15
QUESTION_MAX = 500


@dataclass(frozen=True)
class PendingAction:
    """Modification préparée par l'assistant : jamais appliquée sans le clic de la personne."""

    kind: str  # "add_idea"
    name: str
    description: str = ""


@dataclass
class AssistantReply:
    title: str
    lines: list[str]
    intent: str
    sources: list[str] = field(default_factory=list)
    action: PendingAction | None = None
    used_ai: bool = False
    notes: list[str] = field(default_factory=list)


def _plain(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


# Ordre important : la première règle qui correspond l'emporte.
INTENTS: tuple[tuple[str, re.Pattern], ...] = (
    ("add_idea", re.compile(r"\b(ajoute|ajouter|enregistre|note|propose)\b.*\bidee\b|\bnouvelle idee\b")),
    ("prepare_meeting", re.compile(r"\bprepar\w*\b.*\breunion\b|\bordre du jour\b|\bprochaine reunion\b")),
    ("week", re.compile(r"\bresum\w*\b.*\b(semaine|discussion\w*|derniers jours)\b|\bcette semaine\b")),
    ("remaining", re.compile(r"\bfonctionnalit\w*\b.*\b(reste\w*|a developper|a faire|manqu\w*|pas (encore )?fait\w*)\b|\breste a (faire|developper)\b")),
    ("progress", re.compile(r"\b(bilan|avancement|progression|ou en est|ou en sommes)\b")),
    ("decisions", re.compile(r"\bdecision\w*\b")),
    ("pending_ideas", re.compile(r"\bidee\w*\b")),
    ("open_questions", re.compile(r"\bquestions?\b.*\b(ouverte|en suspens|en attente|non resolue|sans reponse)s?\b")),
    ("search", re.compile(r"^(cherche|recherche|trouve)\b")),
)


def detect_intent(question: str) -> str:
    text = _plain(question)
    for name, pattern in INTENTS:
        if pattern.search(text):
            return name
    return "free"


def extract_idea(question: str) -> tuple[str, str]:
    """« Ajoute cette idée : un boss qui vole — il change de phase » → (nom, description)."""
    text = question.strip()
    match = re.search(r"[:«\"“]\s*(.+)$", text)
    body = match.group(1) if match else re.sub(r"(?i)^.*?\bid[ée]e\b\s*", "", text)
    body = body.strip(" »\"”.")
    parts = re.split(r"\s+[—–-]\s+|\.\s+", body, maxsplit=1)
    name = parts[0].strip()[:80]
    description = parts[1].strip()[:1000] if len(parts) > 1 else ""
    return name, description


class ProjectAssistant:
    def __init__(
        self,
        *,
        meetings: MeetingRepository,
        ideas: IdeaRepository,
        tierlists: TierListRepository,
        debates: DebateRepository,
        tracker: TrackerService,
        ai=None,  # services.ai_assistant.AIAssistant ou None
        passages: Callable[[str], list] | None = None,
        now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
    ) -> None:
        self.meetings = meetings
        self.ideas = ideas
        self.tierlists = tierlists
        self.debates = debates
        self.tracker = tracker
        self.ai = ai
        self.passages = passages
        self._now = now

    # --- Point d'entrée -----------------------------------------------------------------

    async def answer(self, question: str, actor: Actor) -> AssistantReply:
        question = " ".join(question.split())[:QUESTION_MAX]
        intent = detect_intent(question)
        if intent == "free":
            reply = await self._free(question, actor)
        else:
            reply = getattr(self, f"_{intent}")(question, actor)
        if len(reply.lines) > MAX_LINES:
            hidden = len(reply.lines) - MAX_LINES
            reply.lines = [*reply.lines[:MAX_LINES], f"… et {hidden} de plus (voir `/suivi` ou `/idee catalogue`)."]
        return reply

    # --- Sources ----------------------------------------------------------------------

    def _tracker_ready(self) -> bool:
        try:
            self.tracker.require_info()
            return True
        except TrackerError:
            return False

    def _recent_meetings(self, actor: Actor, days: int | None = None, limit: int = 10):
        result = []
        since = (self._now() - dt.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S") if days else None
        for meeting in self.meetings.list(limit=limit, guild_id=actor.guild_id):
            if since and (meeting.started_at or meeting.created_at) < since:
                continue
            detail = self.meetings.detail(meeting.id)
            if detail:
                result.append(detail)
        return result

    def _meeting_decisions(self, actor: Actor, days: int | None = None) -> list[str]:
        lines = []
        for detail in self._recent_meetings(actor, days):
            for note in detail.notes:
                if note.kind == "decision":
                    lines.append(f"{_md(note.content)} · réunion #{detail.meeting.id} {_md(detail.meeting.title)} ({note.created_at[:10]})")
        return lines

    def _debate_decisions(self, actor: Actor) -> list[str]:
        return [
            f"{_md(d.decision or '')} · débat #{d.id} {_md(d.title)} ({(d.decided_at or '')[:10]})"
            for d in self.debates.list(actor.guild_id, statuses=("decided",), limit=10)
        ]

    # --- Intentions -------------------------------------------------------------------

    def _progress(self, question: str, actor: Actor) -> AssistantReply:
        lines, sources = [], []
        if self._tracker_ready():
            p = self.tracker.progress()
            lines.append(f"**Fonctionnalités** : {p['done']}/{p['features']} terminées ({p['percent']} %).")
            if p["by_status"]:
                lines.append("Par statut : " + ", ".join(f"{_md(k)} {v}" for k, v in p["by_status"].items()))
            lines.append(f"Idées au classeur : {p['ideas']} · décisions : {p['decisions']} · questions ouvertes : {p['open_questions']}")
            if p["pending_changes"]:
                lines.append(f"{p['pending_changes']} modification(s) du suivi en attente de validation (`/suivi en-attente`).")
            sources.append("classeur de suivi (FEATURES, IDEAS, DECISIONS, OPEN_QUESTIONS)")
        else:
            lines.append("Le classeur de suivi n'est pas encore importé : pas de compte des fonctionnalités (`/suivi importer`).")
        active = self.ideas.list(actor.guild_id, statuses=(ACTIVE,))
        open_tl = self.tierlists.list(actor.guild_id, statuses=("open",))
        open_debates = self.debates.list(actor.guild_id, statuses=("open", "voting", "proposed"))
        lines.append(f"Idées en attente sur Discord : {len(active)} · tier lists ouvertes : {len(open_tl)} · débats ouverts : {len(open_debates)}")
        actions = [a for d in self._recent_meetings(actor, limit=10) for a in d.actions if a.status == "open"]
        lines.append(f"Actions de réunion encore ouvertes : {len(actions)}")
        sources.append("réunions, idées, tier lists et débats de Samae")
        return AssistantReply("Bilan de l'avancement", lines, "progress", sources)

    def _remaining(self, question: str, actor: Actor) -> AssistantReply:
        if not self._tracker_ready():
            return AssistantReply("Fonctionnalités restantes", [
                "Je n'ai pas encore le classeur de suivi : un administrateur doit l'importer avec `/suivi importer`."
            ], "remaining")
        rows = self.tracker.by_status(FEATURES, remaining=True)
        lines = [f"`{_md(r.key)}` {_md(r.title)} · {_md(r.status or 'sans statut')}" for r in rows]
        title = f"Fonctionnalités restantes ({len(rows)})"
        return AssistantReply(title, lines or ["Toutes les fonctionnalités du classeur sont terminées."], "remaining", ["classeur › FEATURES"])

    def _decisions(self, question: str, actor: Actor) -> AssistantReply:
        lines, sources = [], []
        if self._tracker_ready():
            for r in self.tracker.latest(DECISIONS, limit=8):
                lines.append(f"`{_md(r.key)}` {_md(r.title)}" + (f" · {_md(r.status)}" if r.status else ""))
            sources.append("classeur › DECISIONS")
        debate = self._debate_decisions(actor)
        meeting = self._meeting_decisions(actor)
        lines += debate[:5] + meeting[:5]
        if debate:
            sources.append("débats décidés")
        if meeting:
            sources.append("décisions de réunion")
        return AssistantReply("Dernières décisions", lines or ["Aucune décision enregistrée pour l'instant."], "decisions", sources)

    def _pending_ideas(self, question: str, actor: Actor) -> AssistantReply:
        ideas = self.ideas.list(actor.guild_id, statuses=(ACTIVE,))
        lines = [f"#{i.id} **{_md(i.name)}** · {_md(i.genre)} / {_md(i.play_style)} · {_md(i.author_name)}" for i in ideas]
        sources = ["idées proposées sur Discord (/idee)"]
        if self._tracker_ready():
            pending = [r for r in self.tracker.rows(IDEAS) if not r.status or not any(
                w in _plain(r.status) for w in ("retenu", "ecart", "rejet", "valid", "abandon"))]
            lines += [f"`{_md(r.key)}` {_md(r.title)} (classeur{', ' + _md(r.status) if r.status else ''})" for r in pending]
            sources.append("classeur › IDEAS")
        return AssistantReply(f"Idées en attente ({len(lines)})", lines or ["Aucune idée en attente."], "pending_ideas", sources)

    def _open_questions(self, question: str, actor: Actor) -> AssistantReply:
        lines, sources = [], []
        if self._tracker_ready():
            rows = self.tracker.by_status(OPEN_QUESTIONS, open_only=True)
            lines += [f"`{_md(r.key)}` {_md(r.title)}" for r in rows]
            sources.append("classeur › OPEN_QUESTIONS")
        for d in self.debates.list(actor.guild_id, statuses=("open", "voting", "proposed")):
            lines.append(f"Débat #{d.id} {_md(d.title)} ({DEBATE_STATUS_LABELS[d.status]})")
        if any(line.startswith("Débat") for line in lines):
            sources.append("débats ouverts")
        return AssistantReply("Questions ouvertes", lines or ["Aucune question ouverte enregistrée."], "open_questions", sources)

    def _prepare_meeting(self, question: str, actor: Actor) -> AssistantReply:
        """Ordre du jour proposé (une suggestion : rien n'est créé sans /reunion creer)."""
        agenda: list[str] = []
        actions = [a for d in self._recent_meetings(actor, limit=5) for a in d.actions if a.status == "open"]
        if actions:
            agenda.append(f"Point sur les {len(actions)} action(s) encore ouverte(s)")
        debates = self.debates.list(actor.guild_id, statuses=("open", "voting", "proposed"))
        for d in debates[:3]:
            agenda.append(f"Trancher le débat #{d.id} : {d.title}")
        ideas = self.ideas.list(actor.guild_id, statuses=(ACTIVE,))
        if ideas and not self.tierlists.list(actor.guild_id, statuses=("open",)):
            agenda.append(f"Classer les {len(ideas)} idée(s) en attente (tier list)")
        elif self.tierlists.list(actor.guild_id, statuses=("open",)):
            agenda.append("Clôturer la tier list en cours et discuter des idées controversées")
        if self._tracker_ready():
            questions = self.tracker.by_status(OPEN_QUESTIONS, open_only=True)
            for r in questions[:3]:
                agenda.append(f"Question ouverte {r.key} : {r.title}")
            pending = self.tracker.pending()
            if pending:
                agenda.append(f"Valider les {len(pending)} modification(s) du suivi en attente")
        if not agenda:
            agenda = ["Tour de table de l'avancement", "Prochaines étapes"]
        steps = " ; ".join(s.replace(";", ",")[:90] for s in agenda[:8])
        lines = [f"{i}. {_md(s)}" for i, s in enumerate(agenda[:8], 1)]
        lines.append("")
        lines.append(f"Pour la créer : `/reunion creer titre:Prochaine réunion etapes:{_md(steps[:700])}`")
        return AssistantReply("Proposition d'ordre du jour", lines, "prepare_meeting",
                              ["actions ouvertes, débats, idées, questions ouvertes"])

    def _week(self, question: str, actor: Actor) -> AssistantReply:
        since = self._now() - dt.timedelta(days=7)
        stamp = since.strftime("%Y-%m-%d %H:%M:%S")
        lines = []
        for detail in self._recent_meetings(actor, days=7):
            m = detail.meeting
            decisions = [n for n in detail.notes if n.kind == "decision"]
            lines.append(
                f"Réunion #{m.id} **{_md(m.title)}** ({(m.started_at or m.created_at)[:10]}) : {len(decisions)} décision(s), "
                f"{len(detail.actions)} action(s)"
            )
            lines += [f"↳ {_md(n.content)}" for n in decisions[:3]]
        ideas = [i for i in self.ideas.list(actor.guild_id) if i.created_at >= stamp]
        if ideas:
            lines.append(f"{len(ideas)} idée(s) proposée(s) : " + ", ".join(_md(i.name) for i in ideas[:8]))
        decided = [d for d in self.debates.list(actor.guild_id, statuses=("decided",)) if (d.decided_at or "") >= stamp]
        lines += [f"Débat #{d.id} décidé : {_md(d.decision or '')}" for d in decided]
        if self._tracker_ready():
            changes = [c for c in self.tracker.history(50) if (c.decided_at or "") >= stamp and c.status == "applied"]
            if changes:
                lines.append(f"{len(changes)} modification(s) appliquée(s) au suivi du projet")
        notes = ["Samae ne lit pas les messages des salons (pas d'intent « contenu des messages ») : "
                 "ce résumé couvre ce qui a été enregistré dans Samae."]
        return AssistantReply("Résumé des 7 derniers jours", lines or ["Rien n'a été enregistré cette semaine."], "week",
                              ["réunions, idées, débats, suivi"], notes=notes)

    def _search(self, question: str, actor: Actor) -> AssistantReply:
        text = re.sub(r"(?i)^(cherche|recherche|trouve)\s*", "", question)
        lines = []
        if self._tracker_ready():
            lines += [f"`{_md(r.key)}` ({r.sheet}) {_md(r.title)}" + (f" · {_md(r.status)}" if r.status else "")
                      for r in self.tracker.search(text, limit=10)]
        from src.services.ideas import normalize_words  # import local : évite un cycle au chargement

        words = normalize_words(text)
        for idea in self.ideas.list(actor.guild_id):
            if words & normalize_words(f"{idea.name} {idea.genre} {idea.play_style} {idea.description}"):
                lines.append(f"Idée #{idea.id} {_md(idea.name)} ({IDEA_STATUS_LABELS[idea.status]})")
        return AssistantReply(f"Résultats pour « {_md(text[:80])} »", lines or ["Aucun résultat."], "search")

    def _add_idea(self, question: str, actor: Actor) -> AssistantReply:
        name, description = extract_idea(question)
        if not name:
            return AssistantReply("Ajouter une idée", ["Précisez l'idée, par exemple : « Ajoute cette idée : un boss qui vole »."], "add_idea")
        lines = [f"Idée préparée : **{_md(name)}**" + (f" — {_md(description)}" if description else ""),
                 "Cliquez sur « Compléter et ajouter » : le formulaire s'ouvre prérempli (style de jeu et genre à préciser). "
                 "Rien n'est enregistré avant votre validation."]
        return AssistantReply("Ajouter une idée", lines, "add_idea", action=PendingAction("add_idea", name, description))

    async def _free(self, question: str, actor: Actor) -> AssistantReply:
        if self.ai is None:
            return AssistantReply("Je n'ai pas compris cette demande", [
                "Sans IA configurée, je réponds à ces demandes :",
                "• « Fais-moi le bilan de notre avancement »",
                "• « Quelles fonctionnalités restent à développer ? »",
                "• « Quelles sont les dernières décisions prises ? »",
                "• « Quelles idées sont encore en attente ? »",
                "• « Quelles questions restent ouvertes ? »",
                "• « Prépare notre prochaine réunion »",
                "• « Résume les discussions de cette semaine »",
                "• « Cherche inventaire »",
                "• « Ajoute cette idée : … »",
            ], "free")
        snapshot = [*self._progress(question, actor).lines, *self._decisions(question, actor).lines[:8]]
        passages = self.passages(question) if self.passages else []
        answer = await self.ai.project(actor.user_id, question, snapshot, passages)
        return AssistantReply("Réponse", answer.text.splitlines(), "free", list(answer.sources), used_ai=True, notes=list(answer.notes))
