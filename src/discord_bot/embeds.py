"""Construction des embeds Discord (messages permanents, notifications et réponses des commandes).

Toutes les couleurs, icônes et pieds de page viennent de ``services.design`` :
chaque embed passe par ``make_embed`` puis ``fit_embed``, qui garantit le
respect des limites de Discord (6000 caractères, 25 champs, 1024 par champ…).
"""

from __future__ import annotations

import datetime as dt

import discord

from src.config import LinkCatalog
from src.database.repositories import AdminAction, BotError, EventRecord, ManagedMessage
from src.gitlab.events import Notification
from src.services import design
from src.services import text_limits as limits
from src.services.content import MessagePayload
from src.services.ai_assistant import AssistantAnswer
from src.services.alerts import Alert
from src.services.backup import BackupInfo
from src.services.design import ICONS, Tone
from src.services.diagnostics import ChannelDiagnostic
from src.services.health import HEALTH_ICONS, HealthCheck, HealthStatus, overall_status
from src.services.history import ActivityStats, Leaderboard
from src.services.monitoring import ErrorRecord
from src.services.project_diagnosis import Diagnosis
from src.services.projects import CATEGORIES as PROJECT_CATEGORIES, PROVIDERS, ParsedRepository, ProjectView, Route as ProjectRoute, describe_filters, visible_routes
from src.services.permanent_messages import ACTION_LABELS, MessageStatus, SyncAction, SyncResult
from src.services.self_roles import SelfRoleCatalog
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
    """Embed aux couleurs du bot. ``footer=None`` met le pied de page standard « Samae »."""
    embed = discord.Embed(
        title=limits.truncate(title.strip(), limits.EMBED_TITLE),
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
        name=limits.truncate(name.strip(), limits.FIELD_NAME) or design.MISSING_VALUE,
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
    """Message d'erreur lisible, sans trace d'appel ni détail interne (la couleur indique la gravité)."""
    return make_embed(title, message, tone=tone)


def success_embed(message: str, title: str = "C'est noté") -> discord.Embed:
    """Confirmation d'une action réussie : ce qui a été fait, et où le voir."""
    return make_embed(title, message, tone=Tone.SUCCESS)


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
    """Boutons d'action d'abord (identifiant fixe, traités par Samae même après un redémarrage),
    puis boutons-liens (ils ouvrent une URL sans traitement par le bot)."""
    if not payload.buttons and not payload.actions:
        return None
    view = discord.ui.View(timeout=None)
    for action in payload.actions:
        view.add_item(discord.ui.Button(style=discord.ButtonStyle.primary, label=action.label, custom_id=action.custom_id))
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


def structure_embed(report: StructureReport, title: str = "Structure du serveur") -> discord.Embed:
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
        f"{len(report.bindings)}/{len(report.channels)} utilisables" + ("" if report.is_complete else " — détails avec `/admin installer`"),
    )
    if messages:
        lines = [f"• **{m.key}** : [voir le message]({_message_link(guild_id, m.channel_id, m.message_id)})" for m in messages]
        value = "\n".join(lines)
    else:
        value = "Aucun message publié pour l'instant."
    add_field(embed, "Messages permanents", value)
    embed.set_footer(text=design.footer(f"dernier /admin installer confirmé : {setup_confirmed_at or 'jamais'}"))
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
            line = f"⏭️ **{item.key}** : salon « {item.channel_key} » non associé (lancez /admin installer)"
        else:
            line = f"🆕 **{item.key}** : pas encore publié dans <#{item.channel_id}> (lancez /admin actualiser)"
        if item.missing_links:
            labels = ", ".join(catalog.label(k) + (" (obligatoire)" if catalog.is_required(k) else "") for k in item.missing_links)
            line += f"\n  ↳ liens à compléter : {labels}"
        if item.error:
            line += f"\n  ↳ dernière erreur : {item.error}"
        lines.append(line)
    _add_lines(embed, lines)
    embed.set_footer(text=design.footer("/admin actualiser met à jour sur place", "/admin reconstruire reconstruit"))
    return fit_embed(embed)


def diagnostic_embed(results: list[ChannelDiagnostic]) -> discord.Embed:
    problems = [r for r in results if not r.ok]
    embed = make_embed("Diagnostic", tone=Tone.WARNING if problems else Tone.SUCCESS)
    lines = []
    for r in results:
        uses = " + ".join({"messages": "messages permanents", "notifications": "notifications Git"}[u] for u in r.uses)
        target = f"<#{r.channel_id}>" if r.channel_id else f"`{r.channel_key}`"
        if r.problem:
            lines.append(f"❌ {target} ({uses}) : {r.problem}")
        elif r.missing:
            lines.append(f"⚠️ {target} ({uses}) : il manque {', '.join(r.missing)}")
        else:
            lines.append(f"✅ {target} ({uses})")
    _add_lines(embed, lines or ["Aucun salon utilisé : lancez /admin installer."])
    embed.set_footer(text=design.footer("permissions calculées par Discord pour le rôle du bot"))
    return fit_embed(embed)


def roles_diagnostic_embed(catalog: SelfRoleCatalog | None, *, welcome_on_join: bool, welcome_delete_hours: int) -> discord.Embed:
    """Choix des rôles (/roles) : rôles proposés, rôles écartés et pourquoi, accueil des nouveaux membres."""
    if catalog is None:
        return make_embed("Choix des rôles", "Serveur inaccessible : impossible de vérifier les rôles.", tone=Tone.WARNING)
    problems = list(catalog.problems)
    tone = Tone.FAILURE if not catalog.bot_can_manage else Tone.WARNING if problems or not catalog.roles else Tone.SUCCESS
    embed = make_embed("Choix des rôles et accueil", tone=tone)
    lines = [f"✅ **{role.name}**" for role in catalog.roles]
    lines += [f"⚠️ **{p.name}** : {p.reason}" for p in problems]
    if not catalog.bot_can_manage:
        lines.insert(0, "❌ Samae n'a pas la permission « Gérer les rôles » : les membres ne peuvent rien choisir.")
    _add_lines(embed, lines or ["Aucun rôle marqué « self_assignable » dans config/server.toml."])
    if welcome_on_join:
        delay = f"supprimé après {welcome_delete_hours} h" if welcome_delete_hours else "jamais supprimé"
        welcome = f"Message d'accueil à chaque arrivée : activé ({delay})."
    else:
        welcome = "Message d'accueil à chaque arrivée : désactivé (`WELCOME_ON_JOIN=false`)."
    add_field(embed, "Accueil des nouveaux membres", welcome)
    embed.set_footer(text=design.footer("rôles proposés par le bouton « Choisir mes rôles » et /roles"))
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


# --- Historique, statistiques, classement, alertes ---------------------------------

EVENT_ICONS = {
    "push": ICONS["push"],
    "merge_request": ICONS["merge_request"],
    "pipeline": ICONS["pipeline"],
    "deployment": "🚀",
}
STATUS_NOTES = {"recorded": "non notifié", "failed": "notification en échec", "skipped": "aucun salon associé"}


def _coverage_text(first_event_at: str | None, total: int) -> str:
    if not total:
        return "aucun événement GitLab enregistré"
    return f"{design.plural(total, 'événement')} reçu(s) depuis {design.relative_time(first_event_at)}"


def events_embed(
    records: list[EventRecord], *, filters: str, coverage: tuple[str | None, int], limit: int
) -> discord.Embed:
    embed = make_embed(f"{ICONS['history']} Événements GitLab", tone=Tone.INFO)
    lines = []
    for r in records:
        note = STATUS_NOTES.get(r.status)
        when = design.relative_time(r.event_at or r.received_at)
        lines.append(
            f"{EVENT_ICONS.get(r.event_type, '•')} {design.md_link(r.title or r.event_type, r.url)} · {when}"
            + (f" · *{note}*" if note else "")
        )
    _add_lines(embed, lines or ["*Aucun événement ne correspond à ces critères.*"])
    embed.set_footer(
        text=design.footer(filters, f"{limit} au plus", f"historique : {_coverage_text(*coverage)}")
    )
    return fit_embed(embed)


def errors_embed(errors: list[BotError], *, period: str) -> discord.Embed:
    embed = make_embed("🧯 Erreurs du bot", tone=Tone.WARNING if errors else Tone.SUCCESS)
    lines = [
        f"`{design.escape_markdown(e.source)}` · {design.plural(e.count, 'fois', 'fois')} · "
        f"dernière {design.relative_time(e.last_at)}\n  ↳ {design.escape_markdown(e.message)}"
        for e in errors
    ]
    _add_lines(embed, lines or ["*Aucune erreur enregistrée sur la période.*"])
    embed.set_footer(text=design.footer(period, "erreurs regroupées par source et message", "détail dans logs/bot.log"))
    return fit_embed(embed)


def _trend(current: int, previous: int | None) -> str:
    if previous is None:
        return ""
    if current == previous:
        return " (=)"
    return f" ({'+' if current > previous else ''}{current - previous})"


def stats_embed(
    stats: ActivityStats,
    *,
    period: str,
    previous: ActivityStats | None,
    weekly: list[tuple[str, int, int]],
    coverage: tuple[str | None, int],
    gitlab_enabled: bool,
) -> discord.Embed:
    embed = make_embed(f"{ICONS['status']} Statistiques GitLab", f"Période : **{period}**")
    p = previous
    add_field(
        embed,
        f"{ICONS['push']} Code",
        f"{design.plural(stats.commits, 'commit')}{_trend(stats.commits, p and p.commits)}\n"
        f"{design.plural(stats.pushes, 'push', 'pushs')}{_trend(stats.pushes, p and p.pushes)}",
        inline=True,
    )
    add_field(
        embed,
        f"{ICONS['merge_request']} Merge requests",
        f"{stats.mr_opened} ouverte(s){_trend(stats.mr_opened, p and p.mr_opened)}\n"
        f"{stats.mr_merged} fusionnée(s){_trend(stats.mr_merged, p and p.mr_merged)}",
        inline=True,
    )
    total = sum(stats.pipelines.values())
    success = stats.pipelines.get("success", 0)
    rate = f" · {success * 100 // total} % de réussite" if total else ""
    add_field(
        embed,
        f"{ICONS['pipeline']} Pipelines terminés",
        f"{success} réussi(s), {stats.pipelines.get('failed', 0)} échoué(s), "
        f"{stats.pipelines.get('canceled', 0)} annulé(s){rate}",
        inline=True,
    )
    if stats.deployments:
        add_field(
            embed,
            "🚀 Déploiements",
            ", ".join(f"{count} {status}" for status, count in sorted(stats.deployments.items())),
            inline=True,
        )
    if weekly:
        lines = [f"`{label}` · {design.plural(commits, 'commit')} · {merged} MR fusionnée(s)" for label, commits, merged in weekly]
        add_field(embed, "🗓️ Par semaine (4 dernières)", "\n".join(lines))
    notes = []
    if previous is not None:
        notes.append("Entre parenthèses : écart avec la période précédente de même durée.")
    notes.append("Le nombre de commits ne mesure ni la qualité ni la quantité du travail.")
    add_field(embed, "ℹ️ À savoir", " ".join(notes))
    source = "webhooks GitLab reçus" if gitlab_enabled else "webhooks GitLab désactivés"
    embed.set_footer(text=design.footer(f"source : {source}", _coverage_text(*coverage)))
    return fit_embed(embed)


def leaderboard_embed(board: Leaderboard, *, period: str, details: bool) -> discord.Embed:
    embed = make_embed("🏅 Contributions de l'équipe", f"Période : **{period}**")
    if not board.contributions:
        embed.description += "\n\n*Aucune contribution mesurée sur la période.*"
    elif board.ranked:
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        lines = []
        for rank, c in board.ranks():
            line = (
                f"{medals.get(rank, f'**{rank}.**')} **{design.escape_markdown(c.name)}** · "
                f"{design.plural(c.merged_mrs, 'MR fusionnée', 'MR fusionnées')} · "
                f"{design.plural(c.approvals, 'approbation')} · {design.plural(c.active_days, 'jour actif', 'jours actifs')}"
            )
            if details:
                line += f"\n  ↳ {c.mrs_opened} MR ouverte(s), {design.plural(c.commits, 'commit')} (indicatif)"
            lines.append(line)
        add_field(embed, "Classement", "\n".join(lines))
    else:
        lines = [
            f"• **{design.escape_markdown(c.name)}** · {c.merged_mrs} MR fusionnée(s) · {c.approvals} approbation(s) · "
            f"{c.active_days} jour(s) actif(s)" + (f" · {c.commits} commit(s)" if details else "")
            for c in board.contributions
        ]
        add_field(embed, "Activité (sans classement)", "\n".join(lines))
        add_field(embed, "Pourquoi pas de classement ?", f"Pas assez de données : {board.reason}.")
    if board.unattributed_merges:
        add_field(
            embed,
            "Non attribuées",
            f"{design.plural(board.unattributed_merges, 'MR fusionnée')} sans auteur indiqué par GitLab.",
        )
    add_field(
        embed,
        "ℹ️ Méthode",
        "Ordre : MR fusionnées (attribuées à leur auteur), puis approbations données aux autres, "
        "puis jours avec au moins un push. Les commits ne comptent pas. "
        "Un outil de suivi, pas une évaluation : le travail hors GitLab (design, réunions, docs) n'apparaît pas.",
    )
    embed.set_footer(text=design.footer("source : webhooks GitLab reçus", design.plural(board.events, "événement")))
    return fit_embed(embed)


def alert_embed(alert: Alert) -> discord.Embed:
    minutes = int(alert.window.total_seconds() // 60)
    embed = make_embed(
        "🚨 Erreur répétée du bot",
        f"{design.plural(alert.occurrences, 'occurrence')} en moins de {minutes} minutes.",
        tone=Tone.FAILURE,
    )
    add_field(embed, "Source", f"`{design.escape_markdown(alert.error.source)}`", inline=True)
    add_field(embed, "Total enregistré", design.plural(alert.error.count, "fois", "fois"), inline=True)
    add_field(embed, "Message", design.escape_markdown(alert.error.message))
    add_field(embed, "Que faire ?", "Consulter `logs/bot.log` sur le serveur et `/etat`. Aucune nouvelle alerte pour cette erreur pendant 1 heure.")
    return fit_embed(embed)


def ai_answer_embed(answer: AssistantAnswer, *, title: str, subject: str = "", url: str | None = None) -> discord.Embed:
    """Réponse de l'IA : texte, sources utilisées, précisions, et mention claire de son origine."""
    embed = make_embed(f"🤖 {title}", answer.text, url=url)
    if subject:
        embed.description = limits.truncate(f"> {design.escape_markdown(subject)}\n\n{answer.text}", limits.EMBED_DESCRIPTION)
    if answer.sources:
        add_field(embed, "📚 Sources transmises", "\n".join(design.escape_markdown(s) for s in answer.sources))
    if answer.notes:
        add_field(embed, "ℹ️ À savoir", "\n".join(f"• {n}" for n in answer.notes))
    origin = f"réponse générée par IA ({answer.model}), à vérifier" if answer.model else "aucune IA appelée"
    embed.set_footer(text=design.footer(origin))
    return fit_embed(embed)


def backup_embed(info: BackupInfo, backups: list[BackupInfo], keep: int) -> discord.Embed:
    embed = make_embed("Sauvegarde créée", f"`{info.path.name}` · {info.size / 1024:.0f} Ko · vérifiée ✅", tone=Tone.SUCCESS)
    lines = [f"• `{b.path.name}` · {design.discord_timestamp(b.created_at, 'R')}" for b in backups[:10]]
    add_field(embed, f"Sauvegardes conservées ({len(backups)}, {keep} au plus)", "\n".join(lines) or "—")
    add_field(
        embed,
        "Restaurer",
        "Jamais depuis Discord : arrêter le bot, remplacer le fichier de la base par une sauvegarde, redémarrer "
        "(procédure : docs/deployment-linux.md).",
    )
    embed.set_footer(text=design.footer("les fichiers restent sur le serveur du bot ; aucune copie n'est envoyée sur Discord"))
    return fit_embed(embed)


# --- Projets suivis (/projet) --------------------------------------------------------------

PROJECT_STATUS_TONES = {"pending": Tone.WARNING, "connected": Tone.SUCCESS, "active": Tone.SUCCESS, "legacy_on": Tone.SUCCESS, "suspended": Tone.WARNING, "legacy_error": Tone.FAILURE}


def _project_line(project: ProjectView) -> str:
    return f"{project.provider_label} · `{design.escape_markdown(project.target)}` · {project.status_label}\nRéférence : `{project.ref}`"


# Projets par page de /projet liste : bien en dessous de la limite de 25 champs et de 6000 caractères
# (un nom fait au plus 80 caractères, un dépôt GitHub 140).
PROJECTS_PER_PAGE = 10


def projects_list_pages(projects: list[ProjectView], *, admin: bool = True) -> list[discord.Embed]:
    """Une page par tranche de ``PROJECTS_PER_PAGE`` projets : aucun projet n'est jamais coupé.

    ``projects`` est déjà filtré pour un membre : la page vide ne dit pas si des projets lui sont masqués.
    """
    if not projects:
        text = (
            "Aucun projet n'est suivi sur ce serveur. Ajoutez-en avec `/projet ajouter` ; les webhooks configurés "
            "dans `.env` apparaissent ici dès qu'ils sont activés."
            if admin
            else "Aucun projet à afficher pour vous sur ce serveur."
        )
        return [make_embed("📦 Projets suivis", text, tone=Tone.NEUTRAL)]
    chunks = [projects[i : i + PROJECTS_PER_PAGE] for i in range(0, len(projects), PROJECTS_PER_PAGE)]
    pages = []
    for number, chunk in enumerate(chunks, start=1):
        title = "📦 Projets suivis" + (f" ({number}/{len(chunks)})" if len(chunks) > 1 else "")
        embed = make_embed(title, f"{design.plural(len(projects), 'projet')} sur ce serveur. Détails : `/projet details`.")
        for project in chunk:
            add_field(embed, design.escape_markdown(project.name), _project_line(project))
        if any(p.legacy for p in projects):
            embed.set_footer(text=design.footer("historique = configuré dans .env, modifiable seulement sur le serveur qui héberge Samae"))
        pages.append(fit_embed(embed))
    return pages


def project_detail_embed(project: ProjectView, *, can_view, admin: bool) -> discord.Embed:
    """``can_view(channel_id)`` : le membre voit-il ce salon ? Un administrateur voit tout, y compris les manques."""
    embed = make_embed(f"📦 {design.escape_markdown(project.name)}", tone=PROJECT_STATUS_TONES.get(project.status, Tone.INFO))
    add_field(embed, "Fournisseur", project.provider_label, inline=True)
    add_field(embed, "Suivi", f"`{design.escape_markdown(project.target)}`", inline=True)
    add_field(embed, "État", project.status_label, inline=True)
    if admin:
        lines = [
            f"{PROJECT_CATEGORIES[r.category]} → " + (f"<#{r.channel_id}>" if r.channel_id is not None else "*salon non associé*")
            + (f" ({describe_filters(r.filters)})" if r.filters else "")
            for r in project.routes
        ]
        add_field(embed, "Salons des notifications", design.bullet_list(lines, "Aucun salon choisi."))
    else:
        shown, hidden = visible_routes(project.routes, can_view)
        lines = [
            f"{PROJECT_CATEGORIES[r.category]} → <#{r.channel_id}>" + (f" ({describe_filters(r.filters)})" if r.filters else "")
            for r in shown
        ]
        text = design.bullet_list(lines, "Aucun salon visible pour vous.")
        if hidden:
            text += f"\n*{hidden} salon(s) masqué(s) : vous n'y avez pas accès.*"
        add_field(embed, "Salons des notifications", text)
    if admin and project.created_by is not None:
        when = design.relative_time(project.created_at) if project.created_at else ""
        add_field(embed, "Ajouté par", f"<@{project.created_by}> {when}".strip())
    if admin and project.note:
        add_field(embed, "Configuration", project.note)
    embed.set_footer(text=design.footer(f"référence {project.ref}"))
    return fit_embed(embed)


def project_route_embed(
    project: ProjectView,
    category: str,
    route: ProjectRoute | None,
    *,
    previous: ProjectRoute | None = None,
    removed: bool = False,
) -> discord.Embed:
    """Résultat de /projet modifier : la catégorie avant et après le changement."""
    label = PROJECT_CATEGORIES[category]
    if removed:
        embed = make_embed(f"{label} : plus publiée", f"Projet **{design.escape_markdown(project.name)}**.", tone=Tone.SUCCESS)
        embed.set_footer(text=design.footer(f"référence {project.ref}"))
        return fit_embed(embed)

    def line(r: ProjectRoute | None) -> str:
        return f"<#{r.channel_id}> · filtres : {describe_filters(r.filters)}" if r is not None else "*non publiée*"

    embed = make_embed(f"{label} : enregistré", f"Projet **{design.escape_markdown(project.name)}**.", tone=Tone.SUCCESS)
    add_field(embed, "Avant", line(previous))
    add_field(embed, "Maintenant", line(route))
    embed.set_footer(text=design.footer(f"référence {project.ref}", "appliqué à la réception des événements"))
    return fit_embed(embed)


# Ce que l'ajout d'un projet permet à ce stade (aucune connexion GitHub App / OAuth GitLab avant B4 et B5).
CONNECTION_NOT_READY = (
    "**Ajout possible : oui.** Le projet sera enregistré sur ce serveur et vous pourrez choisir ses salons "
    "et ses filtres avec `/projet modifier`.\n"
    "**Surveillance : pas encore.** La connexion à {provider} n'est pas encore disponible dans Samae : "
    "aucune notification ne sera publiée pour ce projet tant qu'elle n'est pas en place. "
    "Aucun token ni mot de passe ne vous sera demandé dans Discord."
)
CONNECTION_GITHUB_NEXT = "\n**Connexion :** ensuite, `/projet configurer` relie le dépôt à la GitHub App de Samae."


def project_add_preview_embed(parsed: ParsedRepository, name: str, guild_name: str, *, warning: str = "") -> discord.Embed:
    """Récapitulatif de /projet ajouter, avant confirmation."""
    provider = PROVIDERS[parsed.provider]
    target = parsed.external_id + (f" · {parsed.instance_url}" if parsed.instance_url else "")
    embed = make_embed("Ajouter ce projet ?", "Rien n'est encore enregistré. Vérifiez puis confirmez.", tone=Tone.INFO)
    add_field(embed, "Fournisseur", provider, inline=True)
    add_field(embed, "Nom du projet", design.escape_markdown(name), inline=True)
    add_field(embed, "Serveur concerné", design.escape_markdown(guild_name), inline=True)
    add_field(embed, "Dépôt", f"`{design.escape_markdown(target)}`")
    next_step = CONNECTION_GITHUB_NEXT if parsed.provider == "github" else ""
    add_field(embed, "À ce stade", CONNECTION_NOT_READY.format(provider=provider) + next_step)
    if warning:
        add_field(embed, "À vérifier", warning)
    embed.set_footer(text=design.footer("expiration : 2 minutes"))
    return fit_embed(embed)


def project_added_embed(project: ProjectView) -> discord.Embed:
    embed = make_embed(
        f"Projet ajouté : {design.escape_markdown(project.name)}",
        f"Référence `{project.ref}`. État : {project.status_label}.\n"
        f"Étape suivante : choisir ses salons avec `/projet modifier projet:{project.ref}`.",
        tone=Tone.SUCCESS,
    )
    embed.set_footer(text=design.footer(f"référence {project.ref}"))
    return fit_embed(embed)


# --- Connexion des projets GitHub (/projet configurer, étape B4) ---------------------------------


def github_connect_embed(installations, *, expires_minutes: int) -> discord.Embed:
    """Lien d'installation de la GitHub App et étapes à suivre. ``installations`` : déjà vérifiées pour ce serveur."""
    embed = make_embed(
        "Connecter un dépôt GitHub",
        "1. **Installer sur GitHub** : choisissez le compte puis le ou les dépôts, et validez.\n"
        "2. GitHub vous demande ensuite d'autoriser Samae : cela permet de vérifier que vous gérez bien "
        "cette installation. Samae n'obtient aucun droit d'écriture et oublie cette autorisation aussitôt.\n"
        "3. Revenez ici et cliquez sur **Choisir le dépôt**.",
        tone=Tone.INFO,
    )
    add_field(
        embed,
        "Sécurité",
        f"Le lien est personnel, valable {expires_minutes} minutes et utilisable une seule fois. "
        "Aucun token ni mot de passe ne vous sera demandé dans Discord.",
    )
    if installations:
        lines = [
            f"• {design.escape_markdown(i.account)} : {len(i.repositories)} dépôt(s), vérifiée le {i.authorized_at[:10]}"
            for i in installations[:10]
        ]
        add_field(embed, "Installations déjà vérifiées pour ce serveur", "\n".join(lines) + "\nVous pouvez directement choisir le dépôt.")
    embed.set_footer(text=design.footer("réservé aux administrateurs"))
    return fit_embed(embed)


def github_connect_preview_embed(repository: str, account: str, guild_name: str, *, existing_ref: str | None, name: str) -> discord.Embed:
    """Récapitulatif avant de relier un dépôt à la GitHub App."""
    embed = make_embed("Connecter ce dépôt ?", "Rien n'est encore relié. Vérifiez puis confirmez.", tone=Tone.INFO)
    add_field(embed, "Dépôt", f"`{design.escape_markdown(repository)}`", inline=True)
    add_field(embed, "Installation", design.escape_markdown(account), inline=True)
    add_field(embed, "Serveur concerné", design.escape_markdown(guild_name), inline=True)
    if existing_ref:
        add_field(embed, "Projet", f"le projet déjà enregistré `{existing_ref}` ({design.escape_markdown(name)}) sera relié")
    else:
        add_field(embed, "Projet", f"un nouveau projet « {design.escape_markdown(name)} » sera enregistré et relié")
    add_field(
        embed,
        "Ensuite",
        "Les événements de ce dépôt seront publiés dans les salons choisis avec `/projet modifier`, "
        "et seulement sur ce serveur.",
    )
    embed.set_footer(text=design.footer("expiration : 2 minutes"))
    return fit_embed(embed)


def github_connected_embed(project: ProjectView) -> discord.Embed:
    if project.routes:
        next_step = "Vous pouvez envoyer un message de test dans ses salons avec le bouton ci-dessous."
    else:
        next_step = f"Étape suivante : choisir ses salons avec `/projet modifier projet:{project.ref}`, puis tester."
    embed = make_embed(
        f"Projet connecté : {design.escape_markdown(project.name)}",
        f"Référence `{project.ref}`. État : {project.status_label}.\n{next_step}",
        tone=Tone.SUCCESS,
    )
    embed.set_footer(text=design.footer(f"référence {project.ref}"))
    return fit_embed(embed)


def project_test_embed(project_name: str, categories) -> discord.Embed:
    """Message de test publié dans un salon de notifications d'un projet."""
    labels = ", ".join(PROJECT_CATEGORIES[c] for c in categories)
    embed = make_embed(
        f"Message de test : {design.escape_markdown(project_name)}",
        f"Samae publiera ici les notifications de ce projet : {labels}.",
        tone=Tone.INFO,
    )
    embed.set_footer(text=design.footer("message de test, aucune action nécessaire"))
    return fit_embed(embed)


def project_diagnosis_embed(project: ProjectView, diagnosis: Diagnosis) -> discord.Embed:
    """Résultat de /projet diagnostic : contrôles dans l'ordre du trajet d'un événement, puis la piste à suivre."""
    status = diagnosis.status
    embed = make_embed(f"{HEALTH_ICONS[status]} Diagnostic : {design.escape_markdown(project.name)}", tone=HEALTH_TONES[status])
    for check in diagnosis.checks:
        add_field(embed, f"{HEALTH_ICONS[check.status]} {check.name}", check.detail)
    add_field(embed, "Piste", diagnosis.hint)
    embed.set_footer(text=design.footer(f"référence {project.ref}", "état vu par Samae à l'instant ; Nginx et le fournisseur ne sont pas vérifiés"))
    return fit_embed(embed)
