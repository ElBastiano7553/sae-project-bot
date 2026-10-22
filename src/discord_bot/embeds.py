"""Construction des embeds Discord (messages permanents, notifications et réponses des commandes).

Toutes les couleurs, icônes et pieds de page viennent de ``services.design`` :
chaque embed passe par ``make_embed`` puis ``fit_embed``, qui garantit le
respect des limites de Discord (6000 caractères, 25 champs, 1024 par champ…).
"""

from __future__ import annotations

import datetime as dt

import discord

from src.config import LinkCatalog
from src.database.repositories import AdminAction, EventRecord, ManagedMessage
from src.gitlab.events import Notification
from src.services import design
from src.services import text_limits as limits
from src.services.content import MessagePayload
from src.services.design import ICONS, Tone
from src.services.diagnostics import ChannelDiagnostic
from src.services.health import HEALTH_ICONS, HealthCheck, HealthStatus, overall_status
from src.services.help import CATEGORIES, HelpEntry
from src.services.monitoring import ErrorRecord
from src.services.permanent_messages import ACTION_LABELS, MessageStatus, SyncAction, SyncResult
from src.services.server_structure import ChannelStatus, StructureReport

COLOR_OK = discord.Color(design.Color.SUCCESS)
COLOR_WARNING = discord.Color(design.Color.WARNING)
COLOR_ERROR = discord.Color(design.Color.FAILURE)
COLOR_INFO = discord.Color(design.Color.INFO)

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
    SyncAction.REBUILD: "🧱",
    SyncAction.SKIP: "⏭️",
    SyncAction.ERROR: "❌",
}

HEALTH_TONES = {
    HealthStatus.OK: Tone.SUCCESS,
    HealthStatus.WARNING: Tone.WARNING,
    HealthStatus.ERROR: Tone.FAILURE,
    HealthStatus.DISABLED: Tone.NEUTRAL,
}


# --- Outils communs --------------------------------------------------------


def make_embed(
    title: str,
    description: str | None = None,
    *,
    tone: str = Tone.INFO,
    footer: str | None = None,
    url: str | None = None,
    timestamp: dt.datetime | None = None,
) -> discord.Embed:
    """Embed aux couleurs du bot. ``footer=None`` met le pied de page standard « SAE Project Bot »."""
    embed = discord.Embed(
        title=limits.truncate(title, limits.EMBED_TITLE),
        description=limits.truncate(description, limits.EMBED_DESCRIPTION) if description else None,
        color=design.tone_color(tone),
        url=url,
        timestamp=timestamp,
    )
    embed.set_footer(text=limits.truncate(footer if footer is not None else design.footer(), limits.EMBED_FOOTER))
    return embed


def add_field(embed: discord.Embed, name: str, value: str, *, inline: bool = False) -> None:
    if len(embed.fields) >= limits.EMBED_FIELDS:
        return
    embed.add_field(
        name=limits.truncate(name, limits.FIELD_NAME) or design.MISSING_VALUE,
        value=limits.truncate(value, limits.FIELD_VALUE) or design.MISSING_VALUE,
        inline=inline,
    )


def fit_embed(embed: discord.Embed) -> discord.Embed:
    """Retire les derniers champs puis raccourcit la description si l'embed dépasse 6000 caractères."""
    while len(embed) > limits.EMBED_TOTAL and embed.fields:
        embed.remove_field(len(embed.fields) - 1)
    overflow = len(embed) - limits.EMBED_TOTAL
    if overflow > 0 and embed.description:
        embed.description = limits.truncate(embed.description, max(len(embed.description) - overflow, 0))
    return embed


def error_embed(message: str, *, title: str = "Action impossible", tone: str = Tone.FAILURE) -> discord.Embed:
    """Message d'erreur lisible, sans trace d'appel ni détail interne."""
    return make_embed(f"{design.TONE_ICONS[tone]} {title}", message, tone=tone)


def _add_lines(embed: discord.Embed, lines: list[str]) -> None:
    chunks = limits.chunk_lines(lines or ["Rien à afficher."], limits.EMBED_DESCRIPTION)
    embed.description = chunks[0]
    for chunk in limits.chunk_lines("\n".join(chunks[1:]).splitlines(), limits.FIELD_VALUE)[:5]:
        embed.add_field(name="(suite)", value=chunk, inline=False)


def _message_link(guild_id: int, channel_id: int, message_id: int) -> str:
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"


# --- Messages permanents ---------------------------------------------------


def payload_to_embed(payload: MessagePayload) -> discord.Embed:
    # Le pied de page sert aussi de référence pour retrouver le message : il n'est jamais modifié ici.
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


# --- Notifications GitLab ---------------------------------------------------


def notification_to_embed(notification: Notification) -> discord.Embed:
    # Horodatage de l'événement d'après GitLab, sinon heure de publication.
    moment = design.parse_timestamp(notification.timestamp) or dt.datetime.now(dt.UTC)
    embed = discord.Embed(
        title=limits.truncate(notification.title, limits.EMBED_TITLE),
        description=limits.truncate(notification.description, limits.EMBED_DESCRIPTION) or None,
        url=notification.url,
        color=notification.color,
        timestamp=moment,
    )
    for field in notification.fields[: limits.EMBED_FIELDS]:
        embed.add_field(
            name=limits.truncate(field.name, limits.FIELD_NAME),
            value=limits.truncate(field.value, limits.FIELD_VALUE) or "—",
            inline=field.inline,
        )
    if notification.footer:
        embed.set_footer(text=limits.truncate(notification.footer, limits.EMBED_FOOTER))
    return fit_embed(embed)


# --- Rapports d'administration ------------------------------------------------


def structure_embed(report: StructureReport, title: str = "🔎 Structure du serveur") -> discord.Embed:
    embed = make_embed(title, tone=Tone.SUCCESS if report.is_complete else Tone.WARNING)
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
    embed.set_footer(text=design.footer(f"{usable}/{len(report.channels)} salons utilisables", "aucun salon créé ni supprimé"))
    return fit_embed(embed)


def sync_embed(results: list[SyncResult], title: str) -> discord.Embed:
    errors = any(r.action is SyncAction.ERROR for r in results)
    skipped = any(r.action is SyncAction.SKIP for r in results)
    embed = make_embed(title, tone=Tone.FAILURE if errors else Tone.WARNING if skipped else Tone.SUCCESS)
    lines = []
    for r in results:
        where = f" dans <#{r.channel_id}>" if r.channel_id else ""
        detail = f" — {r.detail}" if r.detail else ""
        lines.append(f"{ACTION_ICONS[r.action]} **{r.key}**{where} : {ACTION_LABELS[r.action]}{detail}")
    _add_lines(embed, lines)
    return fit_embed(embed)


def health_embed(
    checks: list[HealthCheck],
    uptime: dt.timedelta,
    version: str,
    *,
    last_error: ErrorRecord | None = None,
    error_count: int = 0,
    title: str = "État du bot",
) -> discord.Embed:
    status = overall_status(checks)
    embed = make_embed(f"{HEALTH_ICONS[status]} {title}", tone=HEALTH_TONES[status])
    for check in checks:
        embed.add_field(name=f"{HEALTH_ICONS[check.status]} {check.name}", value=check.detail, inline=True)
    if last_error is not None:
        when = design.discord_timestamp(last_error.at, "R")
        add_field(
            embed,
            f"🧯 Dernière erreur ({error_count} depuis le démarrage)",
            f"{when} · `{last_error.source}`\n{last_error.message}",
        )
    else:
        add_field(embed, "🧯 Dernière erreur", "aucune depuis le démarrage")
    embed.set_footer(
        text=design.footer(
            f"version {version}",
            f"en ligne depuis {design.format_uptime(uptime)}",
            "état mesuré à l'instant, pas une surveillance continue",
        )
    )
    return fit_embed(embed)


def config_embed(
    *,
    settings_lines: list[str],
    report: StructureReport,
    messages: list[ManagedMessage],
    guild_id: int,
    setup_confirmed_at: str | None,
) -> discord.Embed:
    embed = make_embed("⚙️ Configuration du bot")
    add_field(embed, "Réglages (.env)", "\n".join(settings_lines))
    add_field(
        embed,
        "Salons",
        f"{len(report.bindings)}/{len(report.channels)} utilisables" + ("" if report.is_complete else " — détails avec `/setup`"),
    )
    if messages:
        lines = [f"• **{m.key}** : [voir le message]({_message_link(guild_id, m.channel_id, m.message_id)})" for m in messages]
        value = "\n".join(lines)
    else:
        value = "Aucun message publié pour l'instant."
    add_field(embed, "Messages permanents", value)
    embed.set_footer(text=design.footer(f"dernier /setup confirmé : {setup_confirmed_at or 'jamais'}"))
    return fit_embed(embed)


def messages_status_embed(statuses: list[MessageStatus], guild_id: int, catalog: LinkCatalog) -> discord.Embed:
    errors = sum(1 for s in statuses if s.error)
    missing = sum(1 for s in statuses if not s.published)
    tone = Tone.FAILURE if errors else Tone.WARNING if missing else Tone.SUCCESS
    embed = make_embed(f"{ICONS['message']} Messages permanents", tone=tone)
    lines = []
    for item in statuses:
        if item.record is not None:
            where = f"[message]({_message_link(guild_id, item.record.channel_id, item.record.message_id)})"
            try:
                state = ACTION_LABELS[SyncAction(item.record.last_action)]
            except ValueError:  # ligne antérieure au lot 1, sans dernière action enregistrée
                state = "publié"
            when = f" · vérifié {design.relative_time(item.record.last_checked_at)}" if item.record.last_checked_at else ""
            line = f"{'❌' if item.error else '✅'} **{item.key}** → {where} · {state}{when}"
        elif item.channel_id is None:
            line = f"⏭️ **{item.key}** : salon « {item.channel_key} » non associé (lancez /setup)"
        else:
            line = f"🆕 **{item.key}** : pas encore publié dans <#{item.channel_id}> (lancez /refresh)"
        if item.missing_links:
            labels = ", ".join(catalog.label(k) + (" (obligatoire)" if catalog.is_required(k) else "") for k in item.missing_links)
            line += f"\n  ↳ liens à compléter : {labels}"
        if item.error:
            line += f"\n  ↳ dernière erreur : {item.error}"
        lines.append(line)
    _add_lines(embed, lines)
    embed.set_footer(text=design.footer("/refresh met à jour sur place", "/admin rebuild reconstruit"))
    return fit_embed(embed)


def diagnostic_embed(results: list[ChannelDiagnostic]) -> discord.Embed:
    problems = [r for r in results if not r.ok]
    embed = make_embed("🩺 Diagnostic", tone=Tone.WARNING if problems else Tone.SUCCESS)
    lines = []
    for r in results:
        uses = " + ".join({"messages": "messages permanents", "notifications": "notifications GitLab"}[u] for u in r.uses)
        target = f"<#{r.channel_id}>" if r.channel_id else f"`{r.channel_key}`"
        if r.problem:
            lines.append(f"❌ {target} ({uses}) : {r.problem}")
        elif r.missing:
            lines.append(f"⚠️ {target} ({uses}) : il manque {', '.join(r.missing)}")
        else:
            lines.append(f"✅ {target} ({uses})")
    _add_lines(embed, lines or ["Aucun salon utilisé : lancez /setup."])
    embed.set_footer(text=design.footer("permissions calculées par Discord pour le rôle du bot"))
    return fit_embed(embed)


def history_embed(actions: list[AdminAction]) -> discord.Embed:
    embed = make_embed(f"{ICONS['history']} Journal d'administration")
    lines = []
    for a in actions:
        target = f" `{a.target}`" if a.target else ""
        outcome = f" — {a.outcome}" if a.outcome else ""
        lines.append(f"{design.relative_time(a.at)} · **{design.escape_markdown(a.user_name)}** : {a.action}{target}{outcome}")
    _add_lines(embed, lines or ["Aucune action enregistrée."])
    embed.set_footer(text=design.footer(f"{len(actions)} dernière(s) action(s)"))
    return fit_embed(embed)


def help_embed(groups: dict[str, list[HelpEntry]], *, show_admin_note: bool) -> discord.Embed:
    embed = make_embed(
        f"{ICONS['help']} Aide du SAE Project Bot",
        "Toutes les réponses aux commandes ne sont visibles que par vous.",
    )
    for category, entries in groups.items():
        lines = [f"`/{e.command}` — {e.description}" + (f"\n  ex. `{e.example}`" if e.example and e.example != f"/{e.command}" else "") for e in entries]
        for index, chunk in enumerate(limits.chunk_lines(lines, limits.FIELD_VALUE)):
            add_field(embed, CATEGORIES[category] + (" (suite)" if index else ""), chunk)
    note = (
        "🔒 Réservé aux membres ayant « Gérer le serveur » ou le rôle d'administration du bot (vérifié par le bot)."
        if show_admin_note
        else "Les commandes d'administration n'apparaissent que pour les administrateurs du bot."
    )
    embed.set_footer(text=design.footer(note))
    return fit_embed(embed)


def project_embed(
    *,
    catalog: LinkCatalog,
    default_branch: str | None,
    latest_pipeline: EventRecord | None,
    merge_requests: list[EventRecord],
    pipelines: list[EventRecord],
    pushes: list[EventRecord],
    gitlab_enabled: bool,
) -> discord.Embed:
    embed = make_embed(f"{ICONS['project']} Projet SAE")
    configured = [design.md_link(catalog.label(k), url) for k, url in catalog.urls.items() if url]
    links = " · ".join(configured) if configured else "*aucun lien renseigné*"
    if catalog.missing:
        required = sum(1 for k in catalog.missing if catalog.is_required(k))
        links += f"\n*{len(catalog.missing)} lien(s) à compléter dans `config/links.toml`, dont {required} obligatoire(s).*"
    add_field(embed, f"{ICONS['link']} Liens", links)

    branch = f"`{design.escape_markdown(default_branch)}` (d'après le dernier webhook GitLab)" if default_branch else "inconnue (aucun webhook reçu)"
    add_field(embed, "🌿 Branche principale", branch, inline=True)
    if default_branch:
        if latest_pipeline is not None:
            label = design.md_link(latest_pipeline.title or "pipeline", latest_pipeline.url)
            when = design.relative_time(latest_pipeline.occurred_at or latest_pipeline.received_at)
            value = f"{label}\n{when}"
        else:
            value = "aucun pipeline reçu sur cette branche"
        add_field(embed, f"{ICONS['pipeline']} Dernier pipeline sur la branche principale", value, inline=True)

    for name, records in (
        (f"{ICONS['merge_request']} Dernières merge requests", merge_requests),
        (f"{ICONS['pipeline']} Derniers pipelines", pipelines),
        (f"{ICONS['push']} Derniers pushs", pushes),
    ):
        add_field(embed, name, _event_lines(records))

    source = "webhooks GitLab reçus par le bot" if gitlab_enabled else "webhooks GitLab désactivés (GITLAB_WEBHOOK_SECRET vide)"
    embed.set_footer(text=design.footer(f"source : {source}", "aucun historique antérieur à leur activation"))
    return fit_embed(embed)


def _event_lines(records: list[EventRecord]) -> str:
    if not records:
        return "*aucun événement enregistré*"
    lines = []
    for r in records:
        when = design.relative_time(r.occurred_at or r.received_at)
        lines.append(f"{design.md_link(r.title or r.event_type, r.url)} · {when}")
    return "\n".join(limits.chunk_lines(lines, limits.FIELD_VALUE)[:1])
