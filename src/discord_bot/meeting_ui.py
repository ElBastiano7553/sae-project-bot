"""Embeds et boutons des réunions (Samae 2.0, lot 1).

Les boutons du panneau sont des ``DynamicItem`` : leur identifiant
(``samae:mtg:<réunion>:<action>:<révision>``) suffit à retrouver la réunion, ils
fonctionnent donc encore après un redémarrage du bot. Aucun bouton ne décide de
rien : il appelle le cog, qui revérifie les droits côté serveur.
"""

from __future__ import annotations

import datetime as dt
import logging
import re

import discord

from src.database.meetings import MeetingDetail
from src.discord_bot.embeds import add_field, fit_embed, make_embed
from src.services import design
from src.services import text_limits as limits
from src.services.design import Color, ICONS, Tone
from src.services.meetings import (
    CANCELLED,
    DECISION,
    FINISHED,
    PLANNED,
    RUNNING,
    STATUS_LABELS,
    MeetingReport,
)

log = logging.getLogger(__name__)

STATUS_STYLE = {
    PLANNED: ("🗓️", Color.SAMAE_VIOLET),
    RUNNING: ("🔴", Color.SAMAE_CRIMSON),
    FINISHED: ("✅", Color.SAMAE_NIGHT),
    CANCELLED: ("⛔", Color.NEUTRAL),
}
STEP_ICONS = {"done": "✅", "current": "▶️", "pending": "▫️"}
# Éléments récents affichés sur le panneau (le compte rendu, lui, contient tout).
PANEL_RECENT = 4
REPORT_PAGE_SIZE = 3800

BUTTON_TEMPLATE = r"samae:mtg:(?P<id>\d+):(?P<action>[a-z]+):(?P<rev>\d+)"
BUTTONS = {
    # action : (libellé, emoji, style)
    "join": ("Rejoindre", "➕", discord.ButtonStyle.secondary),
    "leave": ("Quitter", "➖", discord.ButtonStyle.secondary),
    "report": ("Compte rendu", "📝", discord.ButtonStyle.secondary),
    "start": ("Démarrer", "▶️", discord.ButtonStyle.success),
    "cancel": ("Annuler", "✖️", discord.ButtonStyle.danger),
    "prev": ("Étape précédente", "⏮️", discord.ButtonStyle.secondary),
    "next": ("Étape suivante", "⏭️", discord.ButtonStyle.primary),
    "finish": ("Terminer", "🏁", discord.ButtonStyle.danger),
}


def _text(value: str) -> str:
    return design.escape_markdown(value)


def _person(user_id: int) -> str:
    return f"<@{int(user_id)}>"


def _elapsed_minutes(started_at: str | None, now: dt.datetime) -> int | None:
    moment = design.parse_timestamp(started_at)
    return None if moment is None else max(int((now - moment).total_seconds() // 60), 0)


def meeting_label(meeting) -> str:
    return f"#{meeting.id} · {meeting.title}"


def panel_embed(detail: MeetingDetail, *, now: dt.datetime | None = None, report_url: str | None = None) -> discord.Embed:
    """Panneau récapitulatif d'une réunion, toujours dans les limites de Discord."""
    now = now or dt.datetime.now(dt.UTC)
    m = detail.meeting
    icon, color = STATUS_STYLE[m.status]
    embed = make_embed(
        f"{ICONS['meeting']} Réunion {meeting_label(m)}",
        _text(m.description) if m.description else None,
        footer=design.footer(f"réunion #{m.id}", f"révision {m.revision}"),
        timestamp=now,
    )
    embed.color = discord.Color(color)

    status = f"{icon} {STATUS_LABELS[m.status].capitalize()}"
    if m.status == RUNNING and m.started_at:
        status += f" · depuis {design.relative_time(m.started_at)}"
    elif m.status in (FINISHED, CANCELLED) and m.ended_at:
        status += f" · {design.relative_time(m.ended_at)}"
    add_field(embed, "Statut", status, inline=True)
    add_field(embed, "Responsable", _person(m.leader_id), inline=True)
    elapsed = _elapsed_minutes(m.started_at, now) if m.status == RUNNING else None
    if elapsed is not None or m.planned_minutes:
        duration = f"{elapsed} min" if elapsed is not None else "pas commencée"
        if m.planned_minutes:
            duration += f" / {m.planned_minutes} min prévues"
            if elapsed is not None and elapsed > m.planned_minutes:
                duration += " ⚠️"
        add_field(embed, "Durée", duration, inline=True)

    add_field(embed, f"Participants ({len(detail.participants)})", ", ".join(_person(p.user_id) for p in detail.participants))
    if m.objectives:
        add_field(embed, "🎯 Objectifs", _text(m.objectives))

    done = sum(1 for s in detail.steps if s.status == "done")
    steps = [
        f"{STEP_ICONS[s.status]} {'**' if s.status == 'current' else ''}{s.position}. {_text(s.title)}{'**' if s.status == 'current' else ''}"
        for s in detail.steps
    ]
    add_field(
        embed,
        f"📋 Ordre du jour ({done}/{len(detail.steps)})",
        limits.chunk_lines(steps, limits.FIELD_VALUE)[0] if steps else "*Aucun point : `/reunion point` pour en ajouter.*",
    )

    decisions = [n for n in detail.notes if n.kind == DECISION]
    reserves = [n for n in detail.notes if n.kind != DECISION]
    if decisions:
        add_field(embed, f"⚖️ Décisions ({len(decisions)})", _recent([_text(n.content) for n in decisions]))
    if reserves:
        add_field(
            embed,
            f"⚠️ Réserves et désaccords ({len(reserves)})",
            _recent([f"{'Réserve' if n.kind == 'reserve' else 'Désaccord'} de {_person(n.author_id)} : {_text(n.content)}" for n in reserves]),
        )
    if detail.actions:
        add_field(
            embed,
            f"📌 Actions ({len(detail.actions)})",
            _recent(
                [
                    f"{_text(a.description)} → {_person(a.assignee_id)}" + (f" · {a.due_date}" if a.due_date else "")
                    for a in detail.actions
                ]
            ),
        )
    if report_url:
        add_field(embed, f"{ICONS['report']} Compte rendu", f"[Lire le compte rendu]({report_url})")
    return fit_embed(embed)


def _recent(lines: list[str]) -> str:
    shown = lines[-PANEL_RECENT:]
    text = "\n".join(f"• {limits.truncate(line, 200)}" for line in shown)
    hidden = len(lines) - len(shown)
    if hidden:
        text = f"*… et {hidden} de plus (voir le compte rendu)*\n{text}"
    return text


def report_embeds(report: MeetingReport) -> list[discord.Embed]:
    """Compte rendu découpé en pages qui respectent les limites de Discord."""
    icon, color = STATUS_STYLE.get(report.status, ("📝", Color.SAMAE_NIGHT))
    lines: list[str] = []
    for section in report.sections:
        lines.append(f"**{section.title}**")
        # Les lignes du compte rendu sont déjà échappées par services.meetings (elles contiennent des mentions).
        if section.lines:
            lines.extend(f"• {line}" for line in section.lines)
        else:
            lines.append("*Rien d'enregistré.*")
        lines.append("")
    pages = limits.chunk_lines(lines, REPORT_PAGE_SIZE) or ["*Compte rendu vide.*"]
    moment = design.parse_timestamp(report.date)
    date = design.discord_timestamp(moment, "f") if moment else report.date
    kind = "compte rendu final" if report.final else "aperçu provisoire (réunion non terminée)"
    embeds = []
    for number, page in enumerate(pages, start=1):
        title = f"{ICONS['report']} Compte rendu · #{report.meeting_id} {_text(report.title)}"
        header = f"{icon} {date}\n\n" if number == 1 else ""
        embed = make_embed(
            title,
            header + page,
            footer=design.footer(f"réunion #{report.meeting_id}", kind, f"page {number}/{len(pages)}"),
        )
        embed.color = discord.Color(color)
        embeds.append(fit_embed(embed))
    return embeds


def progress_embed(detail: MeetingDetail, *, now: dt.datetime | None = None) -> discord.Embed:
    """Progression : étapes avec leurs horaires, temps écoulé par rapport à la durée prévue."""
    now = now or dt.datetime.now(dt.UTC)
    m = detail.meeting
    icon, color = STATUS_STYLE[m.status]
    done = sum(1 for s in detail.steps if s.status == "done")
    total = len(detail.steps)
    embed = make_embed(f"📊 Progression · {meeting_label(m)}", footer=design.footer(f"réunion #{m.id}"))
    embed.color = discord.Color(color)
    filled = round(10 * done / total) if total else 0
    summary = [f"{icon} {STATUS_LABELS[m.status].capitalize()}", f"`{'▰' * filled}{'▱' * (10 - filled)}` {done}/{total} étapes traitées"]
    elapsed = _elapsed_minutes(m.started_at, now) if m.status == RUNNING else None
    if elapsed is not None:
        summary.append(f"⏱️ {elapsed} min écoulées" + (f" sur {m.planned_minutes} prévues" if m.planned_minutes else ""))
        if m.planned_minutes and total and done < total:
            summary.append(f"≈ {max(m.planned_minutes - elapsed, 0) // max(total - done, 1)} min par étape restante")
    embed.description = "\n".join(summary)
    lines = []
    for s in detail.steps:
        when = ""
        if s.status == "done" and s.done_at:
            when = f" · terminée {design.relative_time(s.done_at)}"
        elif s.status == "current" and s.started_at:
            when = f" · commencée {design.relative_time(s.started_at)}"
        lines.append(f"{STEP_ICONS[s.status]} {s.position}. {_text(s.title)}{when}")
    for chunk in limits.chunk_lines(lines, limits.FIELD_VALUE)[:3] if lines else []:
        add_field(embed, "Ordre du jour", chunk)
    return fit_embed(embed)


def meeting_list_embed(meetings, title: str) -> discord.Embed:
    embed = make_embed(f"{ICONS['meeting']} {title}", tone=Tone.INFO)
    embed.color = discord.Color(Color.SAMAE_VIOLET)
    lines = [
        f"{STATUS_STYLE[m.status][0]} **#{m.id}** {_text(m.title)} · {_person(m.leader_id)} · {STATUS_LABELS[m.status]}"
        f" · {design.relative_time(m.started_at or m.created_at)}"
        for m in meetings
    ]
    embed.description = limits.chunk_lines(lines, limits.EMBED_DESCRIPTION)[0] if lines else "*Aucune réunion.*"
    return fit_embed(embed)


def success_embed(message: str, title: str = "C'est noté") -> discord.Embed:
    return make_embed(f"✅ {title}", message, tone=Tone.SUCCESS)


# --- Boutons -------------------------------------------------------------------


class MeetingButton(discord.ui.DynamicItem[discord.ui.Button], template=BUTTON_TEMPLATE):
    """Bouton persistant du panneau. ``revision`` : état de la réunion quand le panneau a été dessiné."""

    def __init__(self, meeting_id: int, action: str, revision: int, *, disabled: bool = False, row: int | None = None) -> None:
        label, emoji, style = BUTTONS[action]
        super().__init__(
            discord.ui.Button(
                label=label,
                emoji=emoji,
                style=style,
                disabled=disabled,
                custom_id=f"samae:mtg:{meeting_id}:{action}:{revision}",
            ),
            row=row,
        )
        self.meeting_id = meeting_id
        self.action = action
        self.revision = revision

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /):
        action = match["action"]
        if action not in BUTTONS:
            raise ValueError(action)  # identifiant inconnu : ignoré par discord.py
        return cls(int(match["id"]), action, int(match["rev"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("MeetingsCog")
        if cog is None:
            await interaction.response.send_message("Les réunions sont momentanément indisponibles.", ephemeral=True)
            return
        await cog.handle_button(interaction, self.meeting_id, self.action, self.revision)


def panel_view(detail: MeetingDetail) -> discord.ui.View:
    """Boutons adaptés à l'état de la réunion (les droits sont revérifiés à chaque clic)."""
    m = detail.meeting
    view = discord.ui.View(timeout=None)

    def add(action: str, row: int) -> None:
        view.add_item(MeetingButton(m.id, action, m.revision, row=row))

    if m.status in (PLANNED, RUNNING):
        add("join", 0)
        add("leave", 0)
    add("report", 0)
    if m.status == PLANNED:
        add("start", 1)
        add("cancel", 1)
    elif m.status == RUNNING:
        add("prev", 1)
        add("next", 1)
        add("finish", 1)
    return view


class PagesView(discord.ui.View):
    """Pagination d'une réponse éphémère (réservée à la personne qui l'a demandée)."""

    def __init__(self, pages: list[discord.Embed], author_id: int, *, timeout: float = 600) -> None:
        super().__init__(timeout=timeout)
        self.pages = pages
        self.index = 0
        self._author_id = author_id
        self._sync()

    def _sync(self) -> None:
        self.previous.disabled = self.index == 0
        self.following.disabled = self.index >= len(self.pages) - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self._author_id

    @discord.ui.button(label="Précédente", emoji="◀️", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.index = max(self.index - 1, 0)
        self._sync()
        await interaction.response.edit_message(embed=self.pages[self.index], view=self)

    @discord.ui.button(label="Suivante", emoji="▶️", style=discord.ButtonStyle.secondary)
    async def following(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.index = min(self.index + 1, len(self.pages) - 1)
        self._sync()
        await interaction.response.edit_message(embed=self.pages[self.index], view=self)
