"""Tableau de bord permanent du salon #gitlab-activite.

Contenu calculé uniquement à partir des webhooks GitLab enregistrés par le bot :
le tableau ne montre jamais de statut « global » deviné. Une MR est affichée comme
ouverte si son dernier événement reçu n'est ni une fusion ni une fermeture ; une
branche est « en échec » si le dernier pipeline reçu sur cette branche a échoué.

Le texte ne contient pas l'heure du rendu : le message n'est modifié sur Discord
que lorsque les données changent (voir ``permanent_messages``).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from src.database.repositories import EventRecord, GitLabEventRepository
from src.services import design
from src.services import text_limits as limits
from src.services.content import EmbedField
from src.services.history import ActivityStats, compute_stats, period_start, previous_period

DASHBOARD_KEY = "tableau-de-bord"
MAX_LINES = 5
CLOSED_MR_ACTIONS = {"merge", "close"}
PIPELINE_LABELS = {
    "success": "✅ réussi",
    "failed": "❌ échoué",
    "canceled": "⏹️ annulé",
    "skipped": "⏭️ ignoré",
    "manual": "✋ en attente d'action manuelle",
    "running": "⏳ en cours",
    "pending": "⏳ en attente",
}
DEPLOYMENT_LABELS = {"success": "✅ réussi", "failed": "❌ échoué", "canceled": "⏹️ annulé", "running": "⏳ en cours"}


@dataclass(frozen=True)
class DashboardData:
    gitlab_enabled: bool
    default_branch: str | None = None
    main_pipeline: EventRecord | None = None
    open_mrs: tuple[EventRecord, ...] = ()
    failing_branches: tuple[EventRecord, ...] = ()
    deployments: tuple[EventRecord, ...] = ()
    week: ActivityStats = ActivityStats()
    previous_week: ActivityStats | None = None
    first_event_at: str | None = None
    total_events: int = 0
    last_event_at: str | None = None


def collect_dashboard(
    repo: GitLabEventRepository, *, default_branch: str | None, gitlab_enabled: bool, now: dt.datetime | None = None
) -> DashboardData:
    now = now or dt.datetime.now(dt.UTC)
    first, total = repo.coverage()
    start = period_start("7j", now)
    previous = previous_period("7j", now)
    records = repo.in_period(previous[0] if previous else start)
    mrs = [r for r in repo.latest_per_subject("merge_request", 50) if r.action not in CLOSED_MR_ACTIONS]
    failing = [r for r in repo.latest_per_subject("pipeline", 50, key="ref") if r.action == "failed"]
    return DashboardData(
        gitlab_enabled=gitlab_enabled,
        default_branch=default_branch,
        main_pipeline=repo.latest_pipeline(default_branch) if default_branch else None,
        open_mrs=tuple(mrs),
        failing_branches=tuple(failing),
        deployments=tuple(repo.latest_per_subject("deployment", MAX_LINES, key="environment")),
        week=compute_stats(records, start),
        previous_week=compute_stats(records, *previous) if previous and first and first <= previous[0] else None,
        first_event_at=first,
        total_events=total,
        last_event_at=repo.last_received_at(),
    )


def dashboard_fields(data: DashboardData) -> list[EmbedField]:
    if not data.gitlab_enabled and not data.total_events:
        return [
            EmbedField(
                "⏸️ Webhooks GitLab désactivés",
                "Le tableau de bord se remplira dès que les webhooks GitLab seront configurés "
                "(`GITLAB_WEBHOOK_SECRET`, voir docs/gitlab-webhooks.md).",
            )
        ]
    if not data.total_events:
        return [EmbedField("⏳ En attente", "Aucun événement GitLab reçu pour l'instant.")]

    fields = [_main_branch_field(data)]
    fields.append(
        EmbedField(
            f"{design.ICONS['merge_request']} Merge requests ouvertes ({len(data.open_mrs)})",
            _lines([_record_line(r) for r in data.open_mrs], "aucune MR ouverte d'après les événements reçus"),
        )
    )
    if data.failing_branches:
        lines = [f"`{design.escape_markdown(r.ref or '?')}` · {_record_line(r)}" for r in data.failing_branches]
        fields.append(EmbedField("❌ Branches dont le dernier pipeline a échoué", _lines(lines, "")))
    if data.deployments:
        lines = [
            f"**{design.escape_markdown(r.environment or '?')}** : {DEPLOYMENT_LABELS.get(r.action or '', r.action or '?')}"
            f" · {design.md_link(r.ref or 'déploiement', r.url)} · {design.relative_time(r.event_at)}"
            for r in data.deployments
        ]
        fields.append(EmbedField("🚀 Derniers déploiements", _lines(lines, "")))
    fields.append(EmbedField("📈 7 derniers jours", _week_text(data.week, data.previous_week)))
    since = design.relative_time(data.first_event_at)
    last = design.relative_time(data.last_event_at)
    fields.append(
        EmbedField(
            "ℹ️ Source",
            f"{design.plural(data.total_events, 'événement')} GitLab reçu(s) par le bot depuis {since}, "
            f"dernier {last}. Rien d'antérieur n'est connu.",
        )
    )
    return fields


def _main_branch_field(data: DashboardData) -> EmbedField:
    name = "🌿 Branche principale"
    if not data.default_branch:
        return EmbedField(name, "inconnue (aucun webhook n'a encore indiqué la branche par défaut)")
    branch = f"`{design.escape_markdown(data.default_branch)}`"
    pipeline = data.main_pipeline
    if pipeline is None:
        return EmbedField(name, f"{branch} · aucun pipeline reçu sur cette branche")
    status = PIPELINE_LABELS.get(pipeline.action or "", pipeline.action or "statut inconnu")
    link = design.md_link("dernier pipeline", pipeline.url)
    return EmbedField(name, f"{branch} · {link} : **{status}** · {design.relative_time(pipeline.event_at)}")


def _record_line(record: EventRecord) -> str:
    title = limits.truncate((record.title or record.event_type).strip(), 90)
    return f"{design.md_link(title, record.url)} · {design.relative_time(record.event_at or record.received_at)}"


def _lines(lines: list[str], empty: str) -> str:
    if not lines:
        return f"*{empty}*"
    shown = lines[:MAX_LINES]
    if len(lines) > MAX_LINES:
        shown.append(f"*… et {len(lines) - MAX_LINES} autre(s)*")
    return limits.chunk_lines(shown, limits.FIELD_VALUE)[0]


def _week_text(week: ActivityStats, previous: ActivityStats | None) -> str:
    def trend(current: int, before: int | None) -> str:
        if before is None or current == before:
            return ""
        return f" ({'+' if current > before else ''}{current - before})"

    p = previous
    lines = [
        f"{design.ICONS['push']} {design.plural(week.commits, 'commit')}{trend(week.commits, p and p.commits)}"
        f" en {design.plural(week.pushes, 'push', 'pushs')}",
        f"{design.ICONS['merge_request']} {week.mr_opened} MR ouverte(s), "
        f"{week.mr_merged} fusionnée(s){trend(week.mr_merged, p and p.mr_merged)}",
        f"{design.ICONS['pipeline']} pipelines : {week.pipelines.get('success', 0)} réussi(s), "
        f"{week.pipelines.get('failed', 0)} échoué(s)",
    ]
    if week.deployments:
        lines.append(f"🚀 {week.deployments.get('success', 0)} déploiement(s) réussi(s), {week.deployments.get('failed', 0)} échoué(s)")
    if previous is not None:
        lines.append("*entre parenthèses : écart avec les 7 jours précédents*")
    return "\n".join(lines)
