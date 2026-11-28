"""Gestionnaire de réunions (1.14.0) : idées, tier lists et débats, sans dépendance à discord.py.

Règles appliquées ici, pour chaque commande et chaque bouton (les boutons ne sont qu'un confort) :

* **idées** : chaque membre propose au plus ``MAX_ACTIVE_IDEAS`` (5) idées actives par serveur ;
  l'auteur (ou un administrateur) modifie ou retire la sienne ; seul un administrateur la
  déclare « retenue » ou « écartée » ; des idées proches sont signalées et peuvent être regroupées ;
* **tier lists** : créées et clôturées par un administrateur ; chaque membre classe chaque idée
  de S à F, peut changer d'avis tant qu'il n'a pas **validé** son bulletin ; les votes restent
  masqués (personne, administrateurs compris, ne voit le bulletin d'un autre) ; à la clôture,
  seuls des résultats agrégés sont calculés : moyennes, égalités, avis extrêmes, désaccords.
  **Aucune idée n'est éliminée automatiquement**, même classée F ;
* **débats** : tout membre ouvre un débat, ajoute un argument pour, contre ou une objection, vote
  au départage, propose une décision ; seule la **confirmation d'un administrateur** (un humain,
  par un bouton) en fait une décision officielle.

Échelle : S = 6, A = 5, B = 4, C = 3, D = 2, F = 1.
"""

from __future__ import annotations

import math
import re
import statistics
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass

from src.database.ideas import (
    TIERS,
    Debate,
    DebateRepository,
    Idea,
    IdeaRepository,
    LimitReached,
    StateConflict,
    TierList,
    TierListRepository,
)
from src.services.design import escape_markdown as _md
from src.services.meetings import Actor, MeetingError, PermissionDenied, RateLimiter, RateLimited, ReportSection

TIER_SCORES = {"S": 6, "A": 5, "B": 4, "C": 3, "D": 2, "F": 1}
SCORE_TIERS = {v: k for k, v in TIER_SCORES.items()}

MAX_ACTIVE_IDEAS = 5
NAME_MAX = 80
STYLE_MAX = 60
GENRE_MAX = 60
DESCRIPTION_MAX = 1000
TIERLIST_TITLE_MAX = 100
MAX_TIERLIST_ITEMS = 25
DEBATE_TITLE_MAX = 150
ENTRY_MAX = 500
MAX_DEBATE_ENTRIES = 100
OPTION_MAX = 80
MAX_OPTIONS = 5
PROPOSAL_MAX = 500
# Idées « proches » : similarité de Jaccard des mots significatifs (nom, style, genre, description).
SIMILARITY_THRESHOLD = 0.34
# Désaccord : écart d'au moins 3 rangs entre le vote le plus haut et le plus bas, ou écart type ≥ 1,5.
DISAGREEMENT_RANGE = 3
DISAGREEMENT_STDEV = 1.5
# Avis extrême : vote éloigné d'au moins 3 points de la moyenne des AUTRES votes (au moins 3 votants).
EXTREME_DISTANCE = 3
MIN_VOTERS_FOR_EXTREMES = 3

ACTIVE, RETAINED, REJECTED, WITHDRAWN = "active", "retained", "rejected", "withdrawn"
IDEA_STATUS_LABELS = {ACTIVE: "en attente", RETAINED: "retenue", REJECTED: "écartée", WITHDRAWN: "retirée"}
DEBATE_STATUS_LABELS = {
    "open": "ouvert", "voting": "vote de départage", "proposed": "décision proposée", "decided": "décidé", "cancelled": "annulé",
}
ENTRY_LABELS = {"pour": "Pour", "contre": "Contre", "objection": "Objection"}

_STOPWORDS = {
    "le", "la", "les", "un", "une", "des", "de", "du", "d", "l", "et", "ou", "a", "au", "aux", "en", "dans", "sur", "avec",
    "pour", "par", "qui", "que", "est", "on", "son", "sa", "ses", "ce", "cet", "cette", "the", "of", "and", "jeu", "game",
}


class IdeaError(MeetingError):
    """Refus expliqué à l'utilisateur."""


def _clean(value: str | None, limit: int, label: str, *, required: bool = False, single_line: bool = True) -> str:
    text = (value or "").strip()
    if single_line:
        text = " ".join(text.split())
    if required and not text:
        raise IdeaError(f"{label} est obligatoire.")
    if len(text) > limit:
        raise IdeaError(f"{label} : {limit} caractères au plus ({len(text)} saisis).")
    return text


def normalize_words(text: str) -> set[str]:
    """Mots significatifs, sans accents ni majuscules ni mots vides : base de la détection des doublons."""
    plain = unicodedata.normalize("NFKD", text.casefold())
    plain = "".join(c for c in plain if not unicodedata.combining(c))
    words = re.findall(r"[a-z0-9]+", plain)
    return {w[:-1] if len(w) > 4 and w.endswith("s") else w for w in words if w not in _STOPWORDS and len(w) > 1}


def similarity(a: Idea, b: Idea) -> float:
    words_a = normalize_words(f"{a.name} {a.play_style} {a.genre} {a.description}")
    words_b = normalize_words(f"{b.name} {b.play_style} {b.genre} {b.description}")
    if not words_a or not words_b:
        return 0.0
    score = len(words_a & words_b) / len(words_a | words_b)
    # Même genre de gameplay ET même style : rapprochement fort, même si les descriptions diffèrent.
    if a.genre and normalize_words(a.genre) == normalize_words(b.genre) and normalize_words(a.play_style) == normalize_words(b.play_style):
        score = max(score, 0.5)
    return round(score, 3)


def idea_label(idea: Idea) -> str:
    return f"#{idea.id} {idea.name}"


# --- Résultats d'une tier list -------------------------------------------------------------


@dataclass(frozen=True)
class IdeaResult:
    idea_id: int
    name: str
    votes: int
    mean: float | None
    median: float | None
    stdev: float | None
    tier: str | None  # rang correspondant à la moyenne arrondie
    distribution: dict[str, int]
    extremes: list[dict]  # [{"tier": "F", "others_mean": 5.3}] — jamais le nom du votant
    disagreement: bool
    rank: int | None = None


def tier_for_mean(mean: float) -> str:
    # Arrondi « au plus proche, .5 vers le haut » (5,5 → S) : pas d'arrondi bancaire.
    return SCORE_TIERS[min(6, max(1, int(math.floor(mean + 0.5))))]


def compute_results(items: list[tuple[int, str]], votes: dict[int, list[tuple[int, str]]]) -> dict:
    """Résultats agrégés et anonymes. ``items`` = [(idée, nom)] dans l'ordre de la tier list."""
    results: list[IdeaResult] = []
    for idea_id, name in items:
        tiers = [tier for _, tier in votes.get(idea_id, [])]
        scores = [TIER_SCORES[t] for t in tiers]
        distribution = {t: tiers.count(t) for t in TIERS if tiers.count(t)}
        if not scores:
            results.append(IdeaResult(idea_id, name, 0, None, None, None, None, {}, [], False))
            continue
        mean = round(statistics.fmean(scores), 2)
        median = float(statistics.median(scores))
        stdev = round(statistics.pstdev(scores), 2) if len(scores) > 1 else 0.0
        extremes = []
        if len(scores) >= MIN_VOTERS_FOR_EXTREMES:
            for i, score in enumerate(scores):
                others = scores[:i] + scores[i + 1:]
                others_mean = statistics.fmean(others)
                if abs(score - others_mean) >= EXTREME_DISTANCE:
                    extremes.append({"tier": SCORE_TIERS[score], "others_mean": round(others_mean, 2)})
        disagreement = len(scores) > 1 and (max(scores) - min(scores) >= DISAGREEMENT_RANGE or stdev >= DISAGREEMENT_STDEV)
        results.append(
            IdeaResult(idea_id, name, len(scores), mean, median, stdev, tier_for_mean(mean), distribution, extremes, disagreement)
        )
    ranked = sorted((r for r in results if r.mean is not None), key=lambda r: (-r.mean, -(r.median or 0), r.idea_id))
    final: list[dict] = []
    rank = 0
    previous = None
    for position, r in enumerate(ranked, 1):
        if r.mean != previous:
            rank = position
            previous = r.mean
        final.append({**r.__dict__, "rank": rank})
    final += [{**r.__dict__} for r in results if r.mean is None]
    by_mean: dict[float, list[int]] = {}
    for r in ranked:
        by_mean.setdefault(r.mean, []).append(r.idea_id)  # type: ignore[arg-type]
    ties = [ids for ids in by_mean.values() if len(ids) > 1]
    return {
        "ideas": final,
        "ties": ties,
        "controversial": [r["idea_id"] for r in final if r.get("disagreement") or r.get("extremes")],
        "low": [r["idea_id"] for r in final if r.get("tier") == "F"],
    }


# --- Services ---------------------------------------------------------------------------------


class IdeaService:
    def __init__(self, repo: IdeaRepository, *, limiter: RateLimiter | None = None) -> None:
        self.repo = repo
        self.limiter = limiter or RateLimiter(30, 60)

    def _throttle(self, actor: Actor) -> None:
        wait = self.limiter.hit(actor.user_id)
        if wait is not None:
            raise RateLimited(f"Trop d'actions en peu de temps : réessayez dans {int(wait) + 1} s.")

    def get(self, idea_id: int, actor: Actor) -> Idea:
        idea = self.repo.get(idea_id)
        if idea is None or idea.guild_id != actor.guild_id:
            raise IdeaError(f"L'idée #{idea_id} n'existe pas.")
        return idea

    def propose(
        self, actor: Actor, *, name: str, play_style: str, genre: str, description: str = "", meeting_id: int | None = None
    ) -> tuple[Idea, list[tuple[Idea, float]]]:
        """Ajoute une idée ; retourne aussi les idées proches déjà au catalogue (pour proposer un regroupement)."""
        self._throttle(actor)
        name = _clean(name, NAME_MAX, "Le nom de l'idée", required=True)
        play_style = _clean(play_style, STYLE_MAX, "Le style de jeu", required=True)
        genre = _clean(genre, GENRE_MAX, "Le genre de gameplay", required=True)
        description = _clean(description, DESCRIPTION_MAX, "La description", single_line=False)
        try:
            idea = self.repo.create(
                actor.guild_id, actor.ref, name=name, play_style=play_style, genre=genre, description=description,
                meeting_id=meeting_id, max_active=MAX_ACTIVE_IDEAS,
            )
        except LimitReached:
            raise IdeaError(
                f"Vous avez déjà {MAX_ACTIVE_IDEAS} idées en attente : retirez-en une (`/idee retirer`) "
                "ou attendez qu'elles soient retenues ou écartées."
            ) from None
        return idea, self.similar_to(idea)

    def similar_to(self, idea: Idea, *, limit: int = 5) -> list[tuple[Idea, float]]:
        others = [o for o in self.repo.list(idea.guild_id, statuses=(ACTIVE, RETAINED)) if o.id != idea.id]
        scored = [(o, similarity(idea, o)) for o in others]
        return sorted([s for s in scored if s[1] >= SIMILARITY_THRESHOLD], key=lambda s: -s[1])[:limit]

    def _require_owner(self, idea: Idea, actor: Actor, what: str) -> None:
        if idea.author_id != actor.user_id and not actor.is_admin:
            raise PermissionDenied(f"Seul l'auteur de l'idée ou un administrateur peut {what}.")

    def edit(self, idea_id: int, actor: Actor, **fields: str | None) -> Idea:
        self._throttle(actor)
        idea = self.get(idea_id, actor)
        self._require_owner(idea, actor, "la modifier")
        limits = {"name": (NAME_MAX, "Le nom"), "play_style": (STYLE_MAX, "Le style"), "genre": (GENRE_MAX, "Le genre"),
                  "description": (DESCRIPTION_MAX, "La description")}
        clean = {
            k: _clean(v, limits[k][0], limits[k][1], required=k != "description", single_line=k != "description")
            for k, v in fields.items() if v is not None and k in limits
        }
        if not clean:
            raise IdeaError("Rien à modifier : indiquez au moins un champ.")
        try:
            return self.repo.update(idea_id, actor.ref, clean, expected_status=(ACTIVE,))
        except StateConflict:
            raise IdeaError("Seule une idée en attente peut être modifiée.") from None

    def withdraw(self, idea_id: int, actor: Actor) -> Idea:
        self._throttle(actor)
        idea = self.get(idea_id, actor)
        self._require_owner(idea, actor, "la retirer")
        if idea.status != ACTIVE:
            raise IdeaError(f"Cette idée est déjà {IDEA_STATUS_LABELS[idea.status]}.")
        return self.repo.set_status(idea_id, actor.ref, WITHDRAWN)

    def set_status(self, idea_id: int, actor: Actor, status: str, reason: str = "") -> Idea:
        """Retenue, écartée ou remise en attente : décision d'un administrateur, jamais automatique."""
        if not actor.is_admin:
            raise PermissionDenied("Seul un administrateur déclare une idée retenue, écartée ou de nouveau en attente.")
        if status not in (ACTIVE, RETAINED, REJECTED):
            raise IdeaError("Statut inconnu.")
        self.get(idea_id, actor)
        try:
            return self.repo.set_status(idea_id, actor.ref, status, detail=_clean(reason, 300, "Le motif"),
                                        max_active=MAX_ACTIVE_IDEAS if status == ACTIVE else None)
        except LimitReached:
            raise IdeaError(f"L'auteur a déjà {MAX_ACTIVE_IDEAS} idées en attente : impossible de la remettre en attente.") from None

    def group(self, idea_id: int, with_id: int, actor: Actor) -> Idea:
        """Regroupe deux idées proches (même famille dans le catalogue) : auteurs des deux idées ou administrateur."""
        a, b = self.get(idea_id, actor), self.get(with_id, actor)
        if a.id == b.id:
            raise IdeaError("Choisissez deux idées différentes.")
        if not actor.is_admin and actor.user_id not in (a.author_id, b.author_id):
            raise PermissionDenied("Seul l'auteur de l'une des deux idées ou un administrateur peut les regrouper.")
        root = b.group_id or b.id
        family_a = a.group_id or a.id
        if family_a == root:
            raise IdeaError("Ces deux idées sont déjà regroupées.")
        members = [i.id for i in self.repo.list(actor.guild_id) if (i.group_id or i.id) == family_a]
        self.repo.set_group(members, root, actor.ref)
        return self.repo.get(a.id)  # type: ignore[return-value]

    def catalogue(self, actor: Actor, *, statuses: tuple[str, ...] = (ACTIVE, RETAINED)) -> list[tuple[Idea, list[Idea]]]:
        """Catalogue collectif : chaque famille (idée racine + idées regroupées), triée par genre puis nom."""
        ideas = self.repo.list(actor.guild_id, statuses=statuses)
        by_id = {i.id: i for i in ideas}
        families: dict[int, list[Idea]] = {}
        for idea in ideas:
            root = idea.group_id if idea.group_id in by_id else idea.id
            families.setdefault(root, []).append(idea)
        result = []
        for root, members in families.items():
            head = by_id[root]
            result.append((head, [m for m in members if m.id != root]))
        return sorted(result, key=lambda f: (f[0].genre.casefold(), f[0].name.casefold()))

    def search(self, actor: Actor, text: str, *, limit: int = 15) -> list[Idea]:
        words = normalize_words(text)
        if not words:
            return []
        ideas = self.repo.list(actor.guild_id, statuses=(ACTIVE, RETAINED, REJECTED))
        scored = []
        for idea in ideas:
            hay = normalize_words(f"{idea.name} {idea.play_style} {idea.genre} {idea.description}")
            hits = len(words & hay)
            if hits:
                scored.append((hits, idea))
        return [i for _, i in sorted(scored, key=lambda s: (-s[0], s[1].id))][:limit]


class TierListService:
    def __init__(self, repo: TierListRepository, ideas: IdeaRepository) -> None:
        self.repo = repo
        self.ideas = ideas

    def get(self, tierlist_id: int, actor: Actor) -> TierList:
        tierlist = self.repo.get(tierlist_id)
        if tierlist is None or tierlist.guild_id != actor.guild_id:
            raise IdeaError(f"La tier list #{tierlist_id} n'existe pas.")
        return tierlist

    def items(self, tierlist: TierList) -> list[Idea]:
        return [i for i in (self.ideas.get(idea_id) for idea_id in self.repo.item_ids(tierlist.id)) if i is not None]

    def create(self, actor: Actor, title: str, *, idea_ids: list[int] | None = None, meeting_id: int | None = None) -> TierList:
        if not actor.is_admin:
            raise PermissionDenied("Seul un administrateur crée une tier list.")
        title = _clean(title, TIERLIST_TITLE_MAX, "Le titre", required=True)
        if idea_ids:
            ideas = [self.ideas.get(i) for i in idea_ids]
            missing = [str(i) for i, idea in zip(idea_ids, ideas, strict=True) if idea is None or idea.guild_id != actor.guild_id]
            if missing:
                raise IdeaError(f"Idée(s) inconnue(s) : #{', #'.join(missing)}.")
            chosen = list(dict.fromkeys(idea_ids))
        else:
            chosen = [i.id for i in self.ideas.list(actor.guild_id, statuses=(ACTIVE,))]
        if not chosen:
            raise IdeaError("Aucune idée en attente à classer : proposez des idées avec `/idee proposer`.")
        if len(chosen) > MAX_TIERLIST_ITEMS:
            raise IdeaError(f"Au plus {MAX_TIERLIST_ITEMS} idées par tier list ({len(chosen)} choisies) : précisez `idees`.")
        return self.repo.create(actor.guild_id, title, chosen, actor.user_id, meeting_id)

    def vote(self, tierlist_id: int, actor: Actor, idea_id: int, tier: str) -> dict[int, str]:
        tierlist = self.get(tierlist_id, actor)
        tier = tier.strip().upper()
        if tier not in TIERS:
            raise IdeaError("Rang inconnu : choisissez S, A, B, C, D ou F.")
        try:
            self.repo.vote(tierlist.id, actor.ref, idea_id, tier)
        except StateConflict:
            voter = self.repo.voter(tierlist.id, actor.user_id)
            if tierlist.status != "open":
                raise IdeaError("Cette tier list est clôturée : les votes ne sont plus modifiables.") from None
            if voter and voter.validated_at:
                raise IdeaError("Vous avez déjà validé votre bulletin : il n'est plus modifiable.") from None
            raise IdeaError(f"L'idée #{idea_id} ne fait pas partie de cette tier list.") from None
        return self.repo.ballot(tierlist.id, actor.user_id)

    def my_ballot(self, tierlist_id: int, actor: Actor) -> tuple[dict[int, str], bool]:
        tierlist = self.get(tierlist_id, actor)
        voter = self.repo.voter(tierlist.id, actor.user_id)
        return self.repo.ballot(tierlist.id, actor.user_id), bool(voter and voter.validated_at)

    def validate(self, tierlist_id: int, actor: Actor) -> None:
        tierlist = self.get(tierlist_id, actor)
        ballot = self.repo.ballot(tierlist.id, actor.user_id)
        missing = [i for i in self.repo.item_ids(tierlist.id) if i not in ballot]
        if missing:
            raise IdeaError(f"Classez d'abord toutes les idées : il en reste {len(missing)} (#{', #'.join(map(str, missing[:10]))}).")
        try:
            self.repo.validate(tierlist.id, actor.user_id)
        except StateConflict:
            raise IdeaError("Bulletin déjà validé, ou tier list clôturée.") from None

    def progress(self, tierlist_id: int, actor: Actor) -> dict:
        """Avancement sans révéler aucun vote : qui a validé, combien de bulletins sont commencés."""
        tierlist = self.get(tierlist_id, actor)
        voters = self.repo.voters(tierlist.id)
        return {
            "validated": [v.voter_id for v in voters if v.validated_at],
            "started": [v.voter_id for v in voters if not v.validated_at],
            "items": len(self.repo.item_ids(tierlist.id)),
        }

    def close(self, tierlist_id: int, actor: Actor) -> TierList:
        if not actor.is_admin:
            raise PermissionDenied("Seul un administrateur clôture une tier list.")
        tierlist = self.get(tierlist_id, actor)
        if tierlist.status != "open":
            raise IdeaError("Cette tier list est déjà clôturée.")
        items = [(i.id, i.name) for i in self.items(tierlist)]
        votes = self.repo.all_votes(tierlist.id, validated_only=True)
        results = compute_results(items, votes)
        results["voters"] = len([v for v in self.repo.voters(tierlist.id) if v.validated_at])
        results["unvalidated"] = len([v for v in self.repo.voters(tierlist.id) if not v.validated_at])
        try:
            return self.repo.close(tierlist.id, results)
        except StateConflict:
            raise IdeaError("Cette tier list vient d'être clôturée.") from None

    def results(self, tierlist_id: int, actor: Actor) -> dict:
        tierlist = self.get(tierlist_id, actor)
        if tierlist.status != "closed" or tierlist.results_data is None:
            raise IdeaError("Les résultats restent masqués jusqu'à la clôture de la tier list (`/tierlist cloturer`).")
        return tierlist.results_data


class DebateService:
    def __init__(self, repo: DebateRepository, ideas: IdeaRepository, tierlists: TierListRepository, *,
                 on_decided: Callable[[Debate, Actor], None] | None = None) -> None:
        self.repo = repo
        self.ideas = ideas
        self.tierlists = tierlists
        self.on_decided = on_decided

    def get(self, debate_id: int, actor: Actor) -> Debate:
        debate = self.repo.get(debate_id)
        if debate is None or debate.guild_id != actor.guild_id:
            raise IdeaError(f"Le débat #{debate_id} n'existe pas.")
        return debate

    def controversial(self, tierlist_id: int, actor: Actor) -> list[dict]:
        tierlist = self.tierlists.get(tierlist_id)
        if tierlist is None or tierlist.guild_id != actor.guild_id:
            raise IdeaError(f"La tier list #{tierlist_id} n'existe pas.")
        data = tierlist.results_data
        if tierlist.status != "closed" or not data:
            raise IdeaError("Les idées controversées sont connues à la clôture de la tier list.")
        wanted = set(data.get("controversial", [])) | {i for group in data.get("ties", []) for i in group}
        return [r for r in data["ideas"] if r["idea_id"] in wanted]

    def open(self, actor: Actor, *, title: str | None = None, idea_id: int | None = None,
             tierlist_id: int | None = None, meeting_id: int | None = None) -> Debate:
        idea = None
        if idea_id is not None:
            idea = self.ideas.get(idea_id)
            if idea is None or idea.guild_id != actor.guild_id:
                raise IdeaError(f"L'idée #{idea_id} n'existe pas.")
            existing = self.repo.find_open_for_idea(actor.guild_id, idea_id)
            if existing:
                raise IdeaError(f"Un débat est déjà ouvert sur cette idée : #{existing.id}.")
        if tierlist_id is not None:
            tl = self.tierlists.get(tierlist_id)
            if tl is None or tl.guild_id != actor.guild_id:
                raise IdeaError(f"La tier list #{tierlist_id} n'existe pas.")
            meeting_id = meeting_id or tl.meeting_id
        subject = _clean(title or (f"Idée {idea_label(idea)}" if idea else ""), DEBATE_TITLE_MAX, "Le sujet", required=True)
        return self.repo.create(actor.guild_id, subject, actor.user_id, idea_id=idea_id, tierlist_id=tierlist_id, meeting_id=meeting_id)

    def add_entry(self, debate_id: int, actor: Actor, kind: str, content: str) -> None:
        debate = self.get(debate_id, actor)
        if kind not in ENTRY_LABELS:
            raise IdeaError("Type d'argument inconnu.")
        content = _clean(content, ENTRY_MAX, "L'argument", required=True, single_line=False)
        try:
            self.repo.add_entry(debate.id, kind, content, actor.ref, max_entries=MAX_DEBATE_ENTRIES)
        except StateConflict:
            raise IdeaError("Ce débat est clos, ou il a atteint le nombre maximal d'arguments.") from None

    def start_vote(self, debate_id: int, actor: Actor, options: list[str]) -> Debate:
        if not actor.is_admin:
            raise PermissionDenied("Seul un administrateur lance un vote de départage.")
        debate = self.get(debate_id, actor)
        labels = list(dict.fromkeys(_clean(o, OPTION_MAX, "Une option", required=True) for o in options if o and o.strip()))
        if not 2 <= len(labels) <= MAX_OPTIONS:
            raise IdeaError(f"Un départage propose de 2 à {MAX_OPTIONS} options différentes (séparées par « ; »).")
        try:
            self.repo.start_vote(debate.id, labels)
        except StateConflict:
            raise IdeaError("Un vote est déjà lancé (ou le débat est clos).") from None
        return self.repo.get(debate.id)  # type: ignore[return-value]

    def vote(self, debate_id: int, actor: Actor, option_id: int) -> None:
        debate = self.get(debate_id, actor)
        try:
            self.repo.vote(debate.id, actor.user_id, option_id)
        except StateConflict:
            raise IdeaError("Aucun vote de départage en cours pour ce débat, ou option inconnue.") from None

    def tally(self, debate: Debate) -> dict:
        options = self.repo.options(debate.id)
        top = max((o.votes for o in options), default=0)
        leaders = [o for o in options if o.votes == top and top > 0]
        return {"options": options, "voters": self.repo.voter_count(debate.id), "leaders": leaders, "tie": len(leaders) > 1}

    def propose(self, debate_id: int, actor: Actor, proposal: str, idea_status: str | None = None) -> Debate:
        debate = self.get(debate_id, actor)
        proposal = _clean(proposal, PROPOSAL_MAX, "La décision proposée", required=True, single_line=False)
        if idea_status not in (None, RETAINED, REJECTED, ACTIVE):
            raise IdeaError("Statut d'idée inconnu.")
        if idea_status and debate.idea_id is None:
            raise IdeaError("Ce débat ne porte pas sur une idée : pas de statut à changer.")
        try:
            return self.repo.propose(debate.id, proposal, idea_status, actor.user_id)
        except StateConflict:
            raise IdeaError("Ce débat est déjà décidé ou annulé.") from None

    def confirm(self, debate_id: int, actor: Actor, expected_proposal: str) -> Debate:
        """Confirmation HUMAINE par un administrateur : la proposition devient la décision officielle."""
        if not actor.is_admin:
            raise PermissionDenied("Seul un administrateur confirme une décision officielle.")
        debate = self.get(debate_id, actor)
        try:
            decided = self.repo.decide(debate.id, actor.user_id, expected_proposal)
        except StateConflict:
            raise IdeaError("La proposition a changé ou a déjà été confirmée : rouvrez le débat pour voir la dernière.") from None
        if decided.idea_id and decided.proposal_idea_status:
            self.ideas.set_status(decided.idea_id, actor.ref, decided.proposal_idea_status, detail=f"débat #{decided.id}")
        if self.on_decided is not None:
            self.on_decided(decided, actor)
        return decided

    def cancel(self, debate_id: int, actor: Actor) -> None:
        debate = self.get(debate_id, actor)
        if not actor.is_admin and debate.created_by != actor.user_id:
            raise PermissionDenied("Seul l'auteur du débat ou un administrateur peut l'annuler.")
        try:
            self.repo.cancel(debate.id)
        except StateConflict:
            raise IdeaError("Ce débat est déjà décidé ou annulé.") from None


# --- Compte rendu de réunion -----------------------------------------------------------------


def meeting_sections(meeting_id: int, guild_id: int | None, ideas: IdeaRepository, tierlists: TierListRepository,
                     debates: DebateRepository) -> list[ReportSection]:
    """Sections ajoutées au compte rendu d'une réunion : idées, tier lists et débats rattachés à cette réunion."""
    sections: list[ReportSection] = []
    linked_ideas: dict[int, Idea] = {}
    tl_lines: list[str] = []
    for tl in reversed(tierlists.list(guild_id, meeting_id=meeting_id)):
        for idea_id in tierlists.item_ids(tl.id):
            idea = ideas.get(idea_id)
            if idea:
                linked_ideas[idea.id] = idea
        data = tl.results_data
        if tl.status == "closed" and data:
            top = [r for r in data["ideas"] if r.get("mean") is not None][:5]
            ranking = ", ".join(f"{_md(r['name'])} ({r['tier']}, {r['mean']:.2f})" for r in top) or "aucun vote"
            tl_lines.append(f"Tier list #{tl.id} « {_md(tl.title)} » ({data.get('voters', 0)} votant(s)) : {ranking}")
            if data.get("ties"):
                tl_lines.append(f"↳ égalités : {len(data['ties'])} ; idées controversées : {len(data.get('controversial', []))}")
        else:
            tl_lines.append(f"Tier list #{tl.id} « {_md(tl.title)} » : {'votes en cours' if tl.status == 'open' else 'annulée'}")
    for idea in ideas.list(guild_id):
        if idea.meeting_id == meeting_id:
            linked_ideas[idea.id] = idea
    retained = [f"{_md(idea_label(i))} ({_md(i.genre)})" for i in linked_ideas.values() if i.status == RETAINED]
    rejected = [f"{_md(idea_label(i))} ({_md(i.genre)})" for i in linked_ideas.values() if i.status == REJECTED]
    decided, open_questions = [], []
    for d in reversed(debates.list(guild_id, meeting_id=meeting_id)):
        if d.status == "decided":
            decided.append(f"Débat #{d.id} {_md(d.title)} → {_md(d.decision or '')}")
        elif d.status in ("open", "voting", "proposed"):
            state = DEBATE_STATUS_LABELS[d.status]
            open_questions.append(f"Débat #{d.id} {_md(d.title)} ({state})")
    if tl_lines:
        sections.append(ReportSection(f"Tier lists ({len(tl_lines)})", tl_lines))
    if linked_ideas:
        sections.append(ReportSection(f"Idées retenues ({len(retained)})", retained))
        sections.append(ReportSection(f"Idées écartées ({len(rejected)})", rejected))
    if decided:
        sections.append(ReportSection(f"Décisions issues des débats ({len(decided)})", decided))
    if open_questions:
        sections.append(ReportSection(f"Questions ouvertes ({len(open_questions)})", open_questions))
    return sections
