"""Détection des catégories et salons existants du serveur Discord.

Le bot ne crée jamais de salon : il compare la structure attendue
(``config/server.toml``) avec un *instantané* du serveur et produit un rapport.
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


@dataclass(frozen=True)
class GuildChannel:
    id: int
    name: str
    type: str  # "text", "voice" ou "other"
    category_id: int | None = None


@dataclass(frozen=True)
class GuildSnapshot:
    categories: tuple[GuildCategory, ...] = ()
    channels: tuple[GuildChannel, ...] = ()


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


@dataclass(frozen=True)
class ChannelResolution:
    spec: ChannelSpec
    status: ChannelStatus
    channel: GuildChannel | None = None
    note: str = ""

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
        return [res for res in self.channels if res.status is not ChannelStatus.OK]

    @property
    def missing_categories(self) -> list[CategoryResolution]:
        return [res for res in self.categories if res.category is None]

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
) -> StructureReport:
    """Associe chaque salon attendu à un salon existant.

    Ordre de recherche pour chaque salon :

    1. l'identifiant déjà enregistré (un salon renommé reste donc reconnu) ;
    2. le nom normalisé, en privilégiant la catégorie attendue.
    """
    saved_bindings = saved_bindings or {}
    categories = tuple(_resolve_category(spec, snapshot) for spec in layout.categories.values())
    category_ids = {res.spec.key: res.category.id for res in categories if res.category}
    channels = tuple(
        _resolve_channel(spec, snapshot, category_ids.get(spec.category_key), saved_bindings.get(spec.key))
        for spec in layout.channels.values()
    )
    return StructureReport(categories=categories, channels=channels)


def _resolve_category(spec: CategorySpec, snapshot: GuildSnapshot) -> CategoryResolution:
    wanted = normalize_name(spec.name)
    matches = [cat for cat in snapshot.categories if normalize_name(cat.name) == wanted]
    if len(matches) == 1:
        return CategoryResolution(spec, matches[0])
    if not matches:
        return CategoryResolution(spec, None, "catégorie introuvable")
    return CategoryResolution(spec, None, f"{len(matches)} catégories portent ce nom")


def _resolve_channel(
    spec: ChannelSpec,
    snapshot: GuildSnapshot,
    expected_category_id: int | None,
    saved_id: int | None,
) -> ChannelResolution:
    by_id = {ch.id: ch for ch in snapshot.channels}
    if saved_id is not None and saved_id in by_id and by_id[saved_id].type == spec.type:
        channel = by_id[saved_id]
        note = "" if normalize_name(channel.name) == normalize_name(spec.name) else f"renommé en « {channel.name} »"
        return _with_category_check(spec, channel, expected_category_id, note)

    wanted = normalize_name(spec.name)
    same_name = [ch for ch in snapshot.channels if normalize_name(ch.name) == wanted]
    right_type = [ch for ch in same_name if ch.type == spec.type]

    if not same_name:
        return ChannelResolution(spec, ChannelStatus.MISSING, note="salon introuvable")
    if not right_type:
        return ChannelResolution(
            spec, ChannelStatus.WRONG_TYPE, same_name[0], note=f"existe mais n'est pas un salon {_type_label(spec.type)}"
        )
    if expected_category_id is not None:
        in_category = [ch for ch in right_type if ch.category_id == expected_category_id]
        if len(in_category) == 1:
            return ChannelResolution(spec, ChannelStatus.OK, in_category[0])
        if len(in_category) > 1:
            return _ambiguous(spec, len(in_category))
    if len(right_type) == 1:
        return _with_category_check(spec, right_type[0], expected_category_id, "")
    return _ambiguous(spec, len(right_type))


def _with_category_check(
    spec: ChannelSpec, channel: GuildChannel, expected_category_id: int | None, note: str
) -> ChannelResolution:
    if expected_category_id is not None and channel.category_id != expected_category_id:
        extra = "hors de la catégorie attendue"
        return ChannelResolution(spec, ChannelStatus.WRONG_CATEGORY, channel, f"{note}, {extra}" if note else extra)
    return ChannelResolution(spec, ChannelStatus.OK, channel, note)


def _ambiguous(spec: ChannelSpec, count: int) -> ChannelResolution:
    return ChannelResolution(
        spec, ChannelStatus.AMBIGUOUS, note=f"{count} salons portent ce nom : renommez-en un ou supprimez le doublon"
    )


def _type_label(channel_type: str) -> str:
    return "vocal" if channel_type == "voice" else "textuel"
