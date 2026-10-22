"""Construction des embeds Discord (messages permanents et rapports des commandes)."""

from __future__ import annotations

import datetime as dt

import discord

from src.database.repositories import ManagedMessage
from src.services import text_limits as limits
from src.services.content import MessagePayload
from src.services.health import HEALTH_ICONS, HealthCheck, HealthStatus, overall_status
from src.services.permanent_messages import ACTION_LABELS, SyncAction, SyncResult
from src.services.server_structure import ChannelStatus, StructureReport

COLOR_OK = discord.Color.green()
COLOR_WARNING = discord.Color.orange()
COLOR_ERROR = discord.Color.red()
COLOR_INFO = discord.Color.blurple()

STATUS_ICONS = {
    ChannelStatus.OK: "✅",
    ChannelStatus.WRONG_CATEGORY: "⚠️",
    ChannelStatus.WRONG_TYPE: "❌",
    ChannelStatus.AMBIGUOUS: "❌",
    ChannelStatus.MISSING: "❌",
}

ACTION_ICONS = {
    SyncAction.UNCHANGED: "✅",
    SyncAction.UPDATE: "✏️",
    SyncAction.RECREATE: "♻️",
    SyncAction.ADOPT: "🔗",
    SyncAction.CREATE: "🆕",
    SyncAction.MOVE: "➡️",
    SyncAction.SKIP: "⏭️",
    SyncAction.ERROR: "❌",
}


# --- Messages permanents ---------------------------------------------------


def payload_to_embed(payload: MessagePayload) -> discord.Embed:
    embed = discord.Embed(title=payload.title, description=payload.description, color=payload.color)
    for field in payload.fields:
        embed.add_field(name=field.name, value=field.value, inline=field.inline)
    embed.set_footer(text=payload.footer)
    return embed


def payload_to_view(payload: MessagePayload) -> discord.ui.View | None:
    """Boutons-liens : ils ouvrent une URL et ne nécessitent aucun traitement par le bot."""
    if not payload.buttons:
        return None
    view = discord.ui.View(timeout=None)
    for button in payload.buttons:
        view.add_item(discord.ui.Button(style=discord.ButtonStyle.link, label=button.label, url=button.url))
    return view


# --- Rapports --------------------------------------------------------------


def structure_embed(report: StructureReport, title: str = "🔎 Structure du serveur") -> discord.Embed:
    color = COLOR_OK if report.is_complete else COLOR_WARNING
    embed = discord.Embed(title=title, color=color)
    lines = []
    for res in report.categories:
        if res.category is None:
            lines.append(f"❌ Catégorie **{res.spec.name}** : {res.note}")
    for res in report.channels:
        target = f"<#{res.channel.id}>" if res.channel and res.usable else f"`{res.spec.name}`"
        note = f" — {res.note}" if res.note else ""
        lines.append(f"{STATUS_ICONS[res.status]} {target}{note}")
    _add_lines(embed, lines)
    usable = len(report.bindings)
    embed.set_footer(text=f"{usable}/{len(report.channels)} salons utilisables · le bot ne crée ni ne supprime aucun salon")
    return embed


def sync_embed(results: list[SyncResult], title: str) -> discord.Embed:
    errors = any(r.action is SyncAction.ERROR for r in results)
    skipped = any(r.action is SyncAction.SKIP for r in results)
    color = COLOR_ERROR if errors else COLOR_WARNING if skipped else COLOR_OK
    embed = discord.Embed(title=title, color=color)
    lines = []
    for r in results:
        where = f" dans <#{r.channel_id}>" if r.channel_id else ""
        detail = f" — {r.detail}" if r.detail else ""
        lines.append(f"{ACTION_ICONS[r.action]} **{r.key}**{where} : {ACTION_LABELS[r.action]}{detail}")
    _add_lines(embed, lines)
    return embed


def health_embed(checks: list[HealthCheck], uptime: dt.timedelta, version: str) -> discord.Embed:
    status = overall_status(checks)
    color = {HealthStatus.OK: COLOR_OK, HealthStatus.WARNING: COLOR_WARNING}.get(status, COLOR_ERROR)
    embed = discord.Embed(title=f"{HEALTH_ICONS[status]} État du bot", color=color)
    for check in checks:
        embed.add_field(name=f"{HEALTH_ICONS[check.status]} {check.name}", value=check.detail, inline=True)
    embed.set_footer(text=f"Version {version} · en ligne depuis {_format_duration(uptime)}")
    return embed


def config_embed(
    *,
    settings_lines: list[str],
    report: StructureReport,
    messages: list[ManagedMessage],
    guild_id: int,
    setup_confirmed_at: str | None,
) -> discord.Embed:
    embed = discord.Embed(title="⚙️ Configuration du bot", color=COLOR_INFO)
    embed.add_field(name="Réglages (.env)", value=limits.truncate("\n".join(settings_lines), limits.FIELD_VALUE), inline=False)
    embed.add_field(
        name="Salons",
        value=f"{len(report.bindings)}/{len(report.channels)} utilisables"
        + ("" if report.is_complete else " — détails avec `/setup`"),
        inline=False,
    )
    if messages:
        lines = [
            f"• **{m.key}** : [voir le message](https://discord.com/channels/{guild_id}/{m.channel_id}/{m.message_id})"
            for m in messages
        ]
        value = limits.truncate("\n".join(lines), limits.FIELD_VALUE)
    else:
        value = "Aucun message publié pour l'instant."
    embed.add_field(name="Messages permanents", value=value, inline=False)
    embed.set_footer(text=f"Dernier /setup confirmé : {setup_confirmed_at or 'jamais'}")
    return embed


def _add_lines(embed: discord.Embed, lines: list[str]) -> None:
    chunks = limits.chunk_lines(lines or ["Rien à afficher."], limits.EMBED_DESCRIPTION)
    embed.description = chunks[0]
    for chunk in limits.chunk_lines("\n".join(chunks[1:]).splitlines(), limits.FIELD_VALUE)[:5]:
        embed.add_field(name="(suite)", value=chunk, inline=False)


def _format_duration(delta: dt.timedelta) -> str:
    minutes, _ = divmod(int(delta.total_seconds()), 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    return f"{days} j {hours} h {minutes} min" if days else f"{hours} h {minutes} min"
