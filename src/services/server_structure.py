"""Détection des catégories et salons existants du serveur Discord.

Ce module ne modifie rien : il compare la structure attendue
(``config/server.toml``) avec un *instantané* du serveur et produit un rapport.
``/setup`` s'en sert tel quel ; ``/refonte`` (``services.server_redesign``) en
déduit un plan de modifications.
Cet instantané est construit à partir de Discord par ``discord_bot.snapshot``,
ce qui permet de tester toute la logique ici sans connexion à Discord.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from enum import StrEnum

from src.config import CategorySpec, ChannelSpec, ServerLayout


@dataclass(frozen=True)
class GuildCategory:
    id: int
    name: str
    position: int = 0
    manageable: bool = True  # le bot peut-il la renommer (« Gérer les salons ») ?
    everyone_can_view: bool = True  # @everyone voit la catégorie (contrôle des catégories privées)


@dataclass(frozen=True)
class GuildChannel:
    id: int
    name: str
    type: str  # "text", "voice" ou "other"
    category_id: int | None = None
    position: int = 0
    manageable: bool = True  # le bot peut-il le renommer et le déplacer ?


@dataclass(frozen=True)
class GuildRole:
    id: int
    name: str
    position: int = 0
    managed: bool = False  # rôle d'un bot ou d'une intégration : jamais modifiable
    is_default: bool = False  # @everyone
    dangerous: tuple[str, ...] = ()  # permissions sensibles accordées (administrator, manage_guild…)


@dataclass(frozen=True)
class GuildSnapshot:
    """Instantané neutre du serveur : tout ce que /setup et /refonte analysent."""

    categories: tuple[GuildCategory, ...] = ()
    channels: tuple[GuildChannel, ...] = ()
    roles: tuple[GuildRole, ...] = ()
    # Permissions du bot sur tout le serveur (noms discord.py : "manage_channels"…).
    bot_permissions: frozenset[str] = frozenset()
    bot_top_role_position: int = 0


class ChannelStatus(StrEnum):
    OK = "ok"
    WRONG_CATEGORY = "wrong_category"  # trouvé, mais dans une autre catégorie : utilisable
    WRONG_TYPE = "wrong_type"  # un salon porte ce nom mais n'est pas du bon type
    AMBIGUOUS = "ambiguous"  # plusieurs salons portent ce nom : le bot ne choisit pas au hasard
    MISSING = "missing"


USABLE_STATUSES = {ChannelStatus.OK, ChannelStatus.WRONG_CATEGORY}


@dataclass(frozen=True)
class CategoryResolution:
    spec: CategorySpec
    category: GuildCategory | None
    note: str = ""
    ambiguous: bool = False


# Comment un salon a été retrouvé, du plus sûr au moins sûr.
MATCH_ID, MATCH_NAME, MATCH_ALIAS = "id", "name", "alias"


@dataclass(frozen=True)
class ChannelResolution:
    spec: ChannelSpec
    status: ChannelStatus
    channel: GuildChannel | None = None
    note: str = ""
    match: str = ""  # MATCH_ID, MATCH_NAME ou MATCH_ALIAS

    @property
    def usable(self) -> bool:
        return self.status in USABLE_STATUSES and self.channel is not None


@dataclass(frozen=True)
class StructureReport:
    categories: tuple[CategoryResolution, ...] = ()
    channels: tuple[ChannelResolution, ...] = field(default_factory=tuple)

    @property
    def bindings(self) -> dict[str, int]:
        """Associations utilisables « fonction → identifiant de salon »."""
        return {res.spec.key: res.channel.id for res in self.channels if res.usable and res.channel}

    @property
    def problems(self) -> list[ChannelResolution]:
        """Salons à signaler ; un salon facultatif absent n'est pas un problème."""
        return [
            res
            for res in self.channels
            if res.status is not ChannelStatus.OK and not (res.spec.optional and res.status is ChannelStatus.MISSING)
        ]

    @property
    def category_bindings(self) -> dict[str, int]:
        return {res.spec.key: res.category.id for res in self.categories if res.category}

    @property
    def missing_categories(self) -> list[CategoryResolution]:
        """Catégories absentes qui devraient contenir au moins un salon non facultatif."""
        needed = {res.spec.category_key for res in self.channels if not res.spec.optional}
        return [res for res in self.categories if res.category is None and res.spec.key in needed]

    @property
    def is_complete(self) -> bool:
        return not self.problems and not self.missing_categories


_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize_name(name: str) -> str:
    """Forme comparable d'un nom de salon ou de catégorie.

    Supprime accents, emojis, majuscules et séparateurs :
    ``"03 · DÉVELOPPEMENT"`` → ``"03-developpement"`` et
    ``"📢・annonces"`` → ``"annonces"``.
    """
    decomposed = unicodedata.normalize("NFKD", name)
    without_marks = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return _NON_ALNUM.sub("-", without_marks.lower()).strip("-")


def resolve_structure(
    layout: ServerLayout,
    snapshot: GuildSnapshot,
    saved_bindings: dict[str, int] | None = None,
    saved_categories: dict[str, int] | None = None,
) -> StructureReport:
    """Associe chaque catégorie et chaque salon attendus à un élément existant.

    Ordre de recherche :

    1. l'identifiant déjà enregistré (un salon renommé reste donc reconnu) ;
    2. le nom normalisé attendu, en privilégiant la catégorie attendue ;
    3. les anciens noms (« aliases ») de config/server.toml.

    Un même salon n'est jamais associé à deux fonctions : en cas de conflit, la
    correspondance la plus sûre l'emporte et l'autre fonction est signalée.
    """
    saved_bindings = saved_bindings or {}
    saved_categories = saved_categories or {}
    categories = _deduplicate_categories(
        [_resolve_category(spec, snapshot, saved_categories.get(spec.key)) for spec in layout.categories.values()]
    )
    category_ids = {res.spec.key: res.category.id for res in categories if res.category}
    channels = [
        _resolve_channel(
            spec,
            snapshot,
            category_ids.get(spec.category_key),
            saved_bindings.get(spec.key),
            layout.categories[spec.category_key].name if spec.category_key in layout.categories else "",
        )
        for spec in layout.channels.values()
    ]
    return StructureReport(categories=tuple(categories), channels=tuple(_deduplicate_channels(channels)))


def _candidates(name: str, aliases: tuple[str, ...]) -> list[tuple[str, str]]:
    """Noms à essayer, dans l'ordre : le nom attendu, puis les anciens noms."""
    result = [(normalize_name(name), MATCH_NAME)]
    for alias in aliases:
        wanted = normalize_name(alias)
        if all(wanted != existing for existing, _ in result):
            result.append((wanted, MATCH_ALIAS))
    return result


def _resolve_category(spec: CategorySpec, snapshot: GuildSnapshot, saved_id: int | None) -> CategoryResolution:
    if saved_id is not None:
        saved = next((cat for cat in snapshot.categories if cat.id == saved_id), None)
        if saved is not None:
            return CategoryResolution(spec, saved)
    for wanted, _ in _candidates(spec.name, spec.aliases):
        matches = [cat for cat in snapshot.categories if normalize_name(cat.name) == wanted]
        if len(matches) == 1:
            return CategoryResolution(spec, matches[0])
        if len(matches) > 1:
            return CategoryResolution(spec, None, f"{len(matches)} catégories portent ce nom", ambiguous=True)
    return CategoryResolution(spec, None, "catégorie introuvable")


def _deduplicate_categories(resolutions: list[CategoryResolution]) -> list[CategoryResolution]:
    seen: dict[int, str] = {}
    result = []
    for res in resolutions:
        if res.category is not None and res.category.id in seen:
            note = f"déjà utilisée pour « {seen[res.category.id]} »"
            result.append(CategoryResolution(res.spec, None, note, ambiguous=True))
            continue
        if res.category is not None:
            seen[res.category.id] = res.spec.name
        result.append(res)
    return result


def _resolve_channel(
    spec: ChannelSpec,
    snapshot: GuildSnapshot,
    expected_category_id: int | None,
    saved_id: int | None,
    category_name: str = "",
) -> ChannelResolution:
    by_id = {ch.id: ch for ch in snapshot.channels}
    if saved_id is not None and saved_id in by_id and by_id[saved_id].type == spec.type:
        channel = by_id[saved_id]
        note = "" if normalize_name(channel.name) == normalize_name(spec.name) else f"renommé en « {channel.name} »"
        return _with_category_check(spec, channel, expected_category_id, note, MATCH_ID, category_name)

    first_wrong_type: GuildChannel | None = None
    for wanted, match in _candidates(spec.name, spec.aliases):
        same_name = [ch for ch in snapshot.channels if normalize_name(ch.name) == wanted]
        right_type = [ch for ch in same_name if ch.type == spec.type]
        if same_name and not right_type and first_wrong_type is None:
            first_wrong_type = same_name[0]
        if not right_type:
            continue
        note = f"ancien nom « {right_type[0].name} »" if match == MATCH_ALIAS else ""
        if expected_category_id is not None:
            in_category = [ch for ch in right_type if ch.category_id == expected_category_id]
            if len(in_category) == 1:
                return ChannelResolution(spec, ChannelStatus.OK, in_category[0], note, match)
            if len(in_category) > 1:
                return _ambiguous(spec, len(in_category))
        if len(right_type) == 1:
            return _with_category_check(spec, right_type[0], expected_category_id, note, match, category_name)
        return _ambiguous(spec, len(right_type))

    if first_wrong_type is not None:
        return ChannelResolution(
            spec, ChannelStatus.WRONG_TYPE, first_wrong_type, note=f"existe mais n'est pas un salon {_type_label(spec.type)}"
        )
    return ChannelResolution(spec, ChannelStatus.MISSING, note="salon facultatif, non créé" if spec.optional else "salon introuvable")


def _deduplicate_channels(resolutions: list[ChannelResolution]) -> list[ChannelResolution]:
    """Un salon trouvé pour deux fonctions reste à la plus sûre (identifiant, puis nom, puis ancien nom)."""
    strength = {MATCH_ID: 0, MATCH_NAME: 1, MATCH_ALIAS: 2}
    owners: dict[int, int] = {}
    for index, res in enumerate(resolutions):
        if not res.usable or res.channel is None:
            continue
        current = owners.get(res.channel.id)
        if current is None or strength.get(res.match, 3) < strength.get(resolutions[current].match, 3):
            owners[res.channel.id] = index
    result = []
    for index, res in enumerate(resolutions):
        if res.usable and res.channel is not None and owners[res.channel.id] != index:
            other = resolutions[owners[res.channel.id]].spec.key
            result.append(
                ChannelResolution(res.spec, ChannelStatus.AMBIGUOUS, note=f"le salon « {res.channel.name} » est déjà utilisé pour « {other} »")
            )
        else:
            result.append(res)
    return result


def _with_category_check(
    spec: ChannelSpec,
    channel: GuildChannel,
    expected_category_id: int | None,
    note: str,
    match: str,
    category_name: str = "",
) -> ChannelResolution:
    if expected_category_id is None and category_name:
        extra = f"catégorie « {category_name} » absente"
        return ChannelResolution(spec, ChannelStatus.WRONG_CATEGORY, channel, f"{note}, {extra}" if note else extra, match)
    if expected_category_id is not None and channel.category_id != expected_category_id:
        extra = "hors de la catégorie attendue"
        return ChannelResolution(spec, ChannelStatus.WRONG_CATEGORY, channel, f"{note}, {extra}" if note else extra, match)
    return ChannelResolution(spec, ChannelStatus.OK, channel, note, match)


def _ambiguous(spec: ChannelSpec, count: int) -> ChannelResolution:
    return ChannelResolution(
        spec, ChannelStatus.AMBIGUOUS, note=f"{count} salons portent ce nom : renommez-en un (aucun n'est choisi au hasard)"
    )


def _type_label(channel_type: str) -> str:
    return "vocal" if channel_type == "voice" else "textuel"
