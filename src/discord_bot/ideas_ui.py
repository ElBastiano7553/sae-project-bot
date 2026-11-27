"""Embeds, formulaires et vues du gestionnaire de réunions (idées, tier lists, débats).

Les vues sont éphémères et réservées à la personne qui les a ouvertes ; chaque clic repasse par
``services.ideas`` qui revérifie les droits et l'état (un bouton n'est jamais une autorisation).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

import discord

from src.database.ideas import TIERS, Debate, Idea, TierList
from src.discord_bot.embeds import add_field, make_embed
from src.services import text_limits as limits
from src.services.design import Tone
from src.services.design import escape_markdown as _md
from src.services.ideas import (
    DEBATE_STATUS_LABELS,
    DESCRIPTION_MAX,
    ENTRY_LABELS,
    GENRE_MAX,
    IDEA_STATUS_LABELS,
    NAME_MAX,
    STYLE_MAX,
    TIER_SCORES,
    IdeaError,
)

log = logging.getLogger(__name__)

TIER_LABELS = {"S": "S · 6 (excellent)", "A": "A · 5", "B": "B · 4", "C": "C · 3", "D": "D · 2", "F": "F · 1 (à revoir)"}
IDEAS_PER_PAGE = 4  # 4 menus + 1 ligne de boutons : maximum de Discord (5 lignes)


def idea_line(idea: Idea, *, with_author: bool = True) -> str:
    parts = [f"**#{idea.id} {_md(idea.name)}**", f"{_md(idea.genre)} · {_md(idea.play_style)}"]
    if with_author:
        parts.append(_md(idea.author_name))
    if idea.status != "active":
        parts.append(IDEA_STATUS_LABELS[idea.status])
    return " · ".join(parts)


def idea_embed(idea: Idea, similar: list[tuple[Idea, float]] = (), history: list[dict] = ()) -> discord.Embed:
    embed = make_embed(f"Idée #{idea.id} · {idea.name}", _md(idea.description) or "Pas de description.")
    add_field(embed, "Style de jeu", _md(idea.play_style), inline=True)
    add_field(embed, "Genre de gameplay", _md(idea.genre), inline=True)
    add_field(embed, "Statut", IDEA_STATUS_LABELS[idea.status], inline=True)
    add_field(embed, "Proposée par", f"{_md(idea.author_name)} · {idea.created_at[:10]}", inline=True)
    if idea.group_id:
        add_field(embed, "Famille", f"regroupée avec l'idée #{idea.group_id}", inline=True)
    if similar:
        add_field(embed, "Idées proches déjà au catalogue", "\n".join(
            f"#{o.id} {_md(o.name)} ({int(score * 100)} %)" for o, score in similar
        ) + "\nRegroupez-les avec `/idee regrouper` si c'est la même idée.")
    if history:
        add_field(embed, "Historique", "\n".join(f"{h['at'][:16]} · {_md(h['user_name'])} · {h['action']}" for h in history[:8]))
    return embed


def catalogue_pages(families: list[tuple[Idea, list[Idea]]], title: str) -> list[discord.Embed]:
    lines = []
    for head, members in families:
        lines.append(idea_line(head))
        lines += [f"  ↳ {idea_line(m)}" for m in members]
    chunks = limits.chunk_lines(lines or ["Aucune idée pour l'instant : `/idee proposer`."], limits.EMBED_DESCRIPTION - 200)
    total = sum(1 + len(m) for _, m in families)
    return [make_embed(f"{title} ({total})", chunk, footer=f"Page {i}/{len(chunks)} · Samae") for i, chunk in enumerate(chunks, 1)]


class IdeaModal(discord.ui.Modal, title="Proposer une idée"):
    """Formulaire Discord : nom, style de jeu, genre de gameplay, description facultative."""

    def __init__(self, on_submit: Callable[[discord.Interaction, dict[str, str]], Awaitable[None]], *,
                 name: str = "", description: str = "", play_style: str = "", genre: str = "", heading: str | None = None) -> None:
        super().__init__(timeout=900, title=heading or "Proposer une idée")
        self._on_submit = on_submit
        self.name = discord.ui.TextInput(label="Nom de l'idée", max_length=NAME_MAX, default=name or None)
        self.play_style = discord.ui.TextInput(label="Style de jeu (ex. : solo, coop, narratif)", max_length=STYLE_MAX,
                                               default=play_style or None)
        self.genre = discord.ui.TextInput(label="Genre de gameplay (ex. : plateforme, roguelike)", max_length=GENRE_MAX,
                                          default=genre or None)
        self.description = discord.ui.TextInput(label="Description (facultative)", style=discord.TextStyle.paragraph,
                                                max_length=DESCRIPTION_MAX, required=False, default=description or None)
        for item in (self.name, self.play_style, self.genre, self.description):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._on_submit(interaction, {
            "name": self.name.value, "play_style": self.play_style.value, "genre": self.genre.value,
            "description": self.description.value or "",
        })


# --- Tier lists -----------------------------------------------------------------------------


def tierlist_embed(tierlist: TierList, items: list[Idea], progress: dict) -> discord.Embed:
    state = {"open": "votes en cours", "closed": "clôturée", "cancelled": "annulée"}[tierlist.status]
    embed = make_embed(f"Tier list #{tierlist.id} · {tierlist.title}", f"État : **{state}**" + (
        f" · réunion #{tierlist.meeting_id}" if tierlist.meeting_id else ""))
    add_field(embed, f"Idées à classer ({len(items)})", "\n".join(f"#{i.id} {_md(i.name)}" for i in items))
    add_field(embed, "Participation (votes masqués)",
              f"{len(progress['validated'])} bulletin(s) validé(s), {len(progress['started'])} en cours.\n"
              + (" ".join(f"<@{u}>" for u in progress["validated"]) or "Personne n'a encore validé."))
    add_field(embed, "Échelle", "S = 6 · A = 5 · B = 4 · C = 3 · D = 2 · F = 1. Une idée classée F n'est jamais éliminée "
              "automatiquement : elle est signalée pour en discuter.")
    return embed


def results_embeds(tierlist: TierList, data: dict) -> list[discord.Embed]:
    lines = []
    for r in data["ideas"]:
        if r.get("mean") is None:
            lines.append(f"— **{_md(r['name'])}** : aucun vote validé")
            continue
        flags = []
        if r.get("disagreement"):
            flags.append("désaccord")
        if r.get("extremes"):
            flags.append(f"{len(r['extremes'])} avis extrême(s)")
        if r.get("tier") == "F":
            flags.append("à rediscuter (non éliminée)")
        dist = " ".join(f"{t}×{n}" for t, n in r["distribution"].items())
        lines.append(f"{r['rank']}. **{r['tier']}** · **{_md(r['name'])}** · moyenne {r['mean']:.2f} · médiane {r['median']:g} "
                     f"· écart type {r['stdev']:.2f} · {dist}" + (f" · ⚠️ {', '.join(flags)}" if flags else ""))
    chunks = limits.chunk_lines(lines or ["Aucune idée."], limits.EMBED_DESCRIPTION - 300)
    embeds = [make_embed(f"Résultats · tier list #{tierlist.id} · {tierlist.title}", chunk) for chunk in chunks]
    last = embeds[-1]
    add_field(last, "Participation", f"{data.get('voters', 0)} bulletin(s) validé(s) comptés"
              + (f" · {data['unvalidated']} non validé(s), non comptés" if data.get("unvalidated") else ""))
    names = {r["idea_id"]: r["name"] for r in data["ideas"]}
    if data.get("ties"):
        add_field(last, "Égalités", "\n".join(" = ".join(_md(names[i]) for i in group) for group in data["ties"]))
    extremes = [f"{_md(r['name'])} : " + ", ".join(f"un {e['tier']} (moyenne des autres {e['others_mean']:.2f})" for e in r["extremes"])
                for r in data["ideas"] if r.get("extremes")]
    if extremes:
        add_field(last, "Avis extrêmes (anonymes)", "\n".join(extremes))
    if data.get("controversial"):
        add_field(last, "À débattre", "Idées controversées ou à égalité : `/debat controverses tierlist:" f"{tierlist.id}`.")
    return embeds


class BallotView(discord.ui.View):
    """Bulletin éphémère : un menu S→F par idée (4 par page), puis « Valider mon bulletin »."""

    def __init__(self, author_id: int, tierlist_id: int, items: list[Idea], ballot: dict[int, str],
                 on_vote: Callable[[int, str], dict[int, str]], on_validate: Callable[[], None], *, timeout: float = 600) -> None:
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.tierlist_id = tierlist_id
        self.items = items
        self.ballot = dict(ballot)
        self._on_vote = on_vote
        self._on_validate = on_validate
        self.page = 0
        self.original: discord.Interaction | None = None
        self._build()

    @property
    def pages(self) -> int:
        return max(1, -(-len(self.items) // IDEAS_PER_PAGE))

    def embed(self) -> discord.Embed:
        done = sum(1 for i in self.items if i.id in self.ballot)
        lines = [f"{'✅' if i.id in self.ballot else '▫️'} #{i.id} {_md(i.name)} → **{self.ballot.get(i.id, '?')}**" for i in self.items]
        embed = make_embed(f"Mon bulletin · tier list #{self.tierlist_id}", "\n".join(lines)[: limits.EMBED_DESCRIPTION],
                           footer=f"Page {self.page + 1}/{self.pages} · {done}/{len(self.items)} classées · votes masqués")
        add_field(embed, "Rappel", "Vous pouvez changer d'avis jusqu'à la validation. Une fois validé, le bulletin est verrouillé.")
        return embed

    def _build(self) -> None:
        self.clear_items()
        start = self.page * IDEAS_PER_PAGE
        for row, idea in enumerate(self.items[start:start + IDEAS_PER_PAGE]):
            select = discord.ui.Select(
                placeholder=limits.truncate(f"#{idea.id} {idea.name}", 100),
                options=[discord.SelectOption(label=TIER_LABELS[t], value=t, default=self.ballot.get(idea.id) == t) for t in TIERS],
                row=row,
            )
            select.callback = self._make_callback(idea.id, select)
            self.add_item(select)
        previous = discord.ui.Button(label="Précédentes", style=discord.ButtonStyle.secondary, row=4, disabled=self.page == 0)
        following = discord.ui.Button(label="Suivantes", style=discord.ButtonStyle.secondary, row=4, disabled=self.page >= self.pages - 1)
        validate = discord.ui.Button(label="Valider mon bulletin", style=discord.ButtonStyle.success, row=4,
                                     disabled=len(self.ballot) < len(self.items))
        previous.callback = self._previous
        following.callback = self._following
        validate.callback = self._validate
        for button in (previous, following, validate):
            self.add_item(button)

    def _make_callback(self, idea_id: int, select: discord.ui.Select):
        async def callback(interaction: discord.Interaction) -> None:
            try:
                self.ballot = self._on_vote(idea_id, select.values[0])
            except IdeaError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
            self._build()
            await interaction.response.edit_message(embed=self.embed(), view=self)

        return callback

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Ce bulletin est celui de quelqu'un d'autre.", ephemeral=True)
            return False
        return True

    async def _previous(self, interaction: discord.Interaction) -> None:
        self.page = max(0, self.page - 1)
        self._build()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def _following(self, interaction: discord.Interaction) -> None:
        self.page = min(self.pages - 1, self.page + 1)
        self._build()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def _validate(self, interaction: discord.Interaction) -> None:
        try:
            self._on_validate()
        except IdeaError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        self.stop()
        embed = self.embed()
        embed.title = f"Bulletin validé · tier list #{self.tierlist_id}"
        await interaction.response.edit_message(embed=embed, view=None)

    async def on_timeout(self) -> None:
        if self.original is not None:
            try:
                await self.original.edit_original_response(view=None)
            except discord.HTTPException:
                log.debug("Bulletin expiré : boutons non retirés", exc_info=True)


# --- Débats ---------------------------------------------------------------------------------


def debate_embed(debate: Debate, entries, tally: dict | None, idea: Idea | None) -> discord.Embed:
    embed = make_embed(f"Débat #{debate.id} · {debate.title}", f"État : **{DEBATE_STATUS_LABELS[debate.status]}**",
                       tone=Tone.SUCCESS if debate.status == "decided" else Tone.INFO)
    if idea:
        add_field(embed, "Idée concernée", idea_line(idea))
    for kind, label in ENTRY_LABELS.items():
        lines = [f"• {_md(e.content)} ({_md(e.author_name)})" for e in entries if e.kind == kind]
        if lines:
            add_field(embed, f"{label} ({len(lines)})", "\n".join(lines))
    if tally and tally["options"]:
        hidden = debate.status == "voting"
        lines = [f"{o.id}. {_md(o.label)}" + ("" if hidden else f" : {o.votes} voix") for o in tally["options"]]
        title = f"Départage ({tally['voters']} votant(s){', résultats affichés à la proposition' if hidden else ''})"
        add_field(embed, title, "\n".join(lines))
        if not hidden and tally["tie"]:
            add_field(embed, "Égalité", "Les options en tête sont à égalité : la décision revient à l'équipe.")
    if debate.proposal and debate.status == "proposed":
        add_field(embed, "Décision proposée (en attente de confirmation d'un administrateur)", _md(debate.proposal))
    if debate.decision:
        add_field(embed, "Décision officielle", f"{_md(debate.decision)}\nConfirmée par <@{debate.decided_by}> le {(debate.decided_at or '')[:16]}")
    return embed


class OptionVoteView(discord.ui.View):
    def __init__(self, author_id: int, options, current: int | None, on_vote: Callable[[int], None], *, timeout: float = 300) -> None:
        super().__init__(timeout=timeout)
        self.author_id = author_id
        select = discord.ui.Select(
            placeholder="Votre choix",
            options=[discord.SelectOption(label=limits.truncate(o.label, 100), value=str(o.id), default=o.id == current) for o in options],
        )

        async def callback(interaction: discord.Interaction) -> None:
            try:
                on_vote(int(select.values[0]))
            except IdeaError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
            await interaction.response.edit_message(content="Vote enregistré (modifiable jusqu'à la proposition de décision).", view=None)

        select.callback = callback
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.author_id


def tier_score_text() -> str:
    return " · ".join(f"{t} = {s}" for t, s in TIER_SCORES.items())
