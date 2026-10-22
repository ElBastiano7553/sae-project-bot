"""Transformation des événements GitLab en notifications neutres (sans discord.py).

Chaque fonction ``format_*`` reçoit le JSON envoyé par GitLab et retourne une
``Notification`` prête à afficher, ou ``None`` si l'événement ne doit pas être
notifié (branche non suivie, action ignorée…). Un JSON incomplet lève
``InvalidPayload`` : le serveur répond alors 400 à GitLab.

Formats de référence : https://docs.gitlab.com/user/project/integrations/webhook_events/
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from src.gitlab.rules import GitLabRules
from src.gitlab.security import clean_text, first_line, plain_text, safe_url
from src.services import text_limits as limits
from src.services.content import EmbedField

# En-tête X-Gitlab-Event → type d'événement géré par le bot.
SUPPORTED_EVENTS = {
    "Push Hook": "push",
    "Merge Request Hook": "merge_request",
    "Pipeline Hook": "pipeline",
}

ZERO_SHA = "0" * 40

COLOR_PUSH = 0x5865F2
COLOR_SUCCESS = 0x57F287
COLOR_FAILURE = 0xED4245
COLOR_NEUTRAL = 0x95A5A6
COLOR_MERGED = 0x9B59B6
COLOR_WARNING = 0xFEE75C

MR_LABELS = {
    "open": ("🆕", "ouverte", COLOR_PUSH),
    "reopen": ("🔁", "rouverte", COLOR_PUSH),
    "update": ("✏️", "mise à jour", COLOR_NEUTRAL),
    "close": ("🚫", "fermée", COLOR_NEUTRAL),
    "merge": ("🎉", "fusionnée", COLOR_MERGED),
    "approved": ("👍", "approuvée", COLOR_SUCCESS),
    "approval": ("👍", "approuvée", COLOR_SUCCESS),
    "unapproved": ("↩️", "approbation retirée", COLOR_WARNING),
    "unapproval": ("↩️", "approbation retirée", COLOR_WARNING),
}

PIPELINE_LABELS = {
    "success": ("✅", "réussi", COLOR_SUCCESS),
    "failed": ("❌", "en échec", COLOR_FAILURE),
    "canceled": ("⏹️", "annulé", COLOR_NEUTRAL),
    "skipped": ("⏭️", "ignoré", COLOR_NEUTRAL),
    "manual": ("✋", "en attente d'une action manuelle", COLOR_WARNING),
    "running": ("⏳", "en cours", COLOR_PUSH),
    "pending": ("⏳", "en attente", COLOR_PUSH),
}

MAX_FAILED_JOBS = 10

# Champs d'une Merge Request dont la modification mérite une notification « mise à jour ».
MR_CHANGE_LABELS = {
    "title": "titre",
    "description": "description",
    "draft": "statut brouillon",
    "work_in_progress": "statut brouillon",
    "labels": "labels",
    "assignees": "assignés",
    "reviewers": "relecteurs",
    "target_branch": "branche cible",
    "milestone_id": "jalon",
}


class InvalidPayload(ValueError):
    """Le JSON reçu ne correspond pas au format attendu pour ce type d'événement."""


@dataclass(frozen=True)
class Notification:
    kind: str
    title: str
    description: str
    color: int
    url: str | None = None
    fields: tuple[EmbedField, ...] = ()
    footer: str = ""


def format_event(kind: str, payload: dict, rules: GitLabRules) -> Notification | None:
    formatter: Callable[[dict, GitLabRules], Notification | None] = {
        "push": format_push,
        "merge_request": format_merge_request,
        "pipeline": format_pipeline,
    }[kind]
    return formatter(payload, rules)


def project_id_of(payload: dict) -> int | None:
    project = payload.get("project")
    value = project.get("id") if isinstance(project, dict) else None
    if value is None:
        value = payload.get("project_id")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


# --- Push ------------------------------------------------------------------


def format_push(payload: dict, rules: GitLabRules) -> Notification | None:
    _expect_kind(payload, "push")
    ref = _require_str(payload, "ref")
    if not ref.startswith("refs/heads/"):
        return None  # les tags ont leur propre événement, non géré en V2
    branch = ref.removeprefix("refs/heads/")
    if not rules.follows_push_branch(branch):
        return None

    project = _project(payload)
    before = str(payload.get("before") or "")
    after = str(payload.get("after") or "")
    commits = payload.get("commits") or []
    if not isinstance(commits, list):
        raise InvalidPayload("« commits » doit être une liste")
    total = payload.get("total_commits_count", len(commits))
    total = total if isinstance(total, int) and total >= 0 else len(commits)
    user = clean_text(payload.get("user_name") or payload.get("user_username"), 100, single_line=True) or "Quelqu'un"
    branch_txt = clean_text(branch, 100, single_line=True)
    branch_plain = plain_text(branch, 100)

    if after == ZERO_SHA:
        return Notification(
            kind="push",
            title=limits.truncate(f"🗑️ Branche supprimée : {branch_plain} · {project.name_plain}", limits.EMBED_TITLE),
            description=f"**{user}** a supprimé la branche `{branch_txt}`.",
            color=COLOR_NEUTRAL,
            url=project.url,
            footer=project.footer,
        )

    created = before == ZERO_SHA
    if not commits and not created:
        return None  # push sans nouveau commit (ex. : retour en arrière forcé) : rien d'utile à dire

    if created and not commits:
        title = f"🌱 Nouvelle branche : {branch_plain} · {project.name_plain}"
    else:
        noun = "commit" if total == 1 else "commits"
        title = f"📤 {total} {noun} sur {branch_plain} · {project.name_plain}"

    lines = []
    # GitLab envoie les commits du plus ancien au plus récent : on affiche les plus récents.
    shown = commits[-rules.max_commits:]
    for commit in reversed(shown):
        if not isinstance(commit, dict):
            raise InvalidPayload("chaque commit doit être un objet")
        sha = str(commit.get("id") or "")[:8]
        message = clean_text(first_line(commit.get("title") or commit.get("message")), 120, single_line=True)
        author = commit.get("author") if isinstance(commit.get("author"), dict) else {}
        author_txt = clean_text(author.get("name"), 60, single_line=True)
        commit_url = safe_url(commit.get("url"))
        ref_txt = f"[`{sha}`]({commit_url})" if commit_url and sha else f"`{sha}`"
        lines.append(f"{ref_txt} {message or '*(sans message)*'}" + (f" — {author_txt}" if author_txt else ""))
    hidden = total - len(shown)
    if hidden > 0:
        lines.append(f"… et {hidden} autre(s) commit(s)")

    intro = f"**{user}** a créé la branche `{branch_txt}`" if created else f"**{user}** a poussé sur `{branch_txt}`"
    description = intro + ("\n\n" + "\n".join(lines) if lines else ".")

    url = project.url
    if project.url and not created and before and after:
        url = f"{project.url}/-/compare/{before[:12]}...{after[:12]}"
    elif project.url and created:
        url = f"{project.url}/-/tree/{after[:12]}"

    return Notification(
        kind="push",
        title=limits.truncate(title, limits.EMBED_TITLE),
        description=limits.truncate(description, limits.EMBED_DESCRIPTION),
        color=COLOR_PUSH,
        url=safe_url(url),
        footer=project.footer,
    )


# --- Merge Requests --------------------------------------------------------


def format_merge_request(payload: dict, rules: GitLabRules) -> Notification | None:
    _expect_kind(payload, "merge_request")
    attrs = _require_dict(payload, "object_attributes")
    action = attrs.get("action")
    if not isinstance(action, str) or action not in rules.mr_actions or action not in MR_LABELS:
        return None
    changed: list[str] = []
    if action == "update":
        if attrs.get("oldrev"):
            return None  # nouveaux commits : déjà signalés par la notification de push
        changes = payload.get("changes") if isinstance(payload.get("changes"), dict) else {}
        changed = list(dict.fromkeys(label for key, label in MR_CHANGE_LABELS.items() if key in changes))
        if not changed:
            return None  # mise à jour technique (statut de fusion recalculé…) : sans intérêt
    project = _project(payload)
    iid = attrs.get("iid")
    if not isinstance(iid, int):
        raise InvalidPayload("« object_attributes.iid » manquant")
    title = clean_text(attrs.get("title"), 200, single_line=True) or "(sans titre)"
    user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
    user_txt = clean_text(user.get("name") or user.get("username"), 100, single_line=True) or "Quelqu'un"
    icon, label, color = MR_LABELS[action]
    draft = bool(attrs.get("draft") or attrs.get("work_in_progress"))

    source = clean_text(attrs.get("source_branch"), 100, single_line=True)
    target = clean_text(attrs.get("target_branch"), 100, single_line=True)
    fields = [EmbedField("Branches", f"`{source or '?'}` → `{target or '?'}`", inline=True)]
    fields.append(EmbedField("Par", user_txt, inline=True))
    if draft:
        fields.append(EmbedField("État", "brouillon (draft)", inline=True))
    if changed:
        fields.append(EmbedField("Modifié", ", ".join(changed), inline=False))

    description = ""
    if action in {"open", "reopen"}:
        description = clean_text(attrs.get("description"), 300)

    plain_title = plain_text(first_line(attrs.get("title")), 200) or "(sans titre)"
    return Notification(
        kind="merge_request",
        title=limits.truncate(f"{icon} MR !{iid} {label} : {plain_title}", limits.EMBED_TITLE),
        description=description or f"**{title}**",
        color=color,
        url=safe_url(attrs.get("url")) or project.url,
        fields=tuple(fields),
        footer=project.footer,
    )


# --- Pipelines -------------------------------------------------------------


def format_pipeline(payload: dict, rules: GitLabRules) -> Notification | None:
    _expect_kind(payload, "pipeline")
    attrs = _require_dict(payload, "object_attributes")
    status = attrs.get("status")
    if not isinstance(status, str) or status not in rules.pipeline_statuses:
        return None
    ref = attrs.get("ref") if isinstance(attrs.get("ref"), str) else ""
    if not rules.follows_pipeline_branch(ref):
        return None
    pipeline_id = attrs.get("id")
    if not isinstance(pipeline_id, int):
        raise InvalidPayload("« object_attributes.id » manquant")
    project = _project(payload)
    icon, label, color = PIPELINE_LABELS.get(status, ("ℹ️", status, COLOR_NEUTRAL))

    fields = [EmbedField("Branche", f"`{clean_text(ref, 100, single_line=True) or '?'}`", inline=True)]
    duration = attrs.get("duration")
    if isinstance(duration, (int, float)) and duration >= 0:
        minutes, seconds = divmod(int(duration), 60)
        fields.append(EmbedField("Durée", f"{minutes} min {seconds:02d} s", inline=True))
    user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
    user_txt = clean_text(user.get("name") or user.get("username"), 100, single_line=True)
    if user_txt:
        fields.append(EmbedField("Lancé par", user_txt, inline=True))

    commit = payload.get("commit") if isinstance(payload.get("commit"), dict) else {}
    sha = str(commit.get("id") or attrs.get("sha") or "")[:8]
    commit_title = clean_text(first_line(commit.get("title") or commit.get("message")), 150, single_line=True)
    if sha or commit_title:
        fields.append(EmbedField("Commit", f"`{sha}` {commit_title}".strip(), inline=False))

    builds = payload.get("builds") if isinstance(payload.get("builds"), list) else []
    failed = [b for b in builds if isinstance(b, dict) and b.get("status") == "failed" and not b.get("allow_failure")]
    if failed:
        names = [
            f"• {clean_text(b.get('name'), 80, single_line=True)} ({clean_text(b.get('stage'), 40, single_line=True)})"
            for b in failed[:MAX_FAILED_JOBS]
        ]
        if len(failed) > MAX_FAILED_JOBS:
            names.append(f"… et {len(failed) - MAX_FAILED_JOBS} autre(s)")
        fields.append(EmbedField("Jobs en échec", limits.truncate("\n".join(names), limits.FIELD_VALUE), inline=False))

    url = safe_url(attrs.get("url"))
    if url is None and project.url:
        url = safe_url(f"{project.url}/-/pipelines/{pipeline_id}")
    return Notification(
        kind="pipeline",
        title=limits.truncate(f"{icon} Pipeline #{pipeline_id} {label} · {project.name_plain}", limits.EMBED_TITLE),
        description="",
        color=color,
        url=url,
        fields=tuple(fields),
        footer=project.footer,
    )


# --- Outils ----------------------------------------------------------------


@dataclass(frozen=True)
class _Project:
    name_plain: str
    url: str | None
    footer: str


def _project(payload: dict) -> _Project:
    project = payload.get("project")
    if not isinstance(project, dict):
        raise InvalidPayload("« project » manquant")
    name = plain_text(first_line(project.get("path_with_namespace") or project.get("name")), 100) or "projet GitLab"
    return _Project(name_plain=name, url=safe_url(project.get("web_url")), footer=limits.truncate(f"GitLab · {name}", 200))


def _expect_kind(payload: dict, kind: str) -> None:
    if payload.get("object_kind") != kind:
        raise InvalidPayload(f"object_kind « {payload.get('object_kind')} » ne correspond pas à l'en-tête ({kind})")


def _require_str(payload: dict, key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise InvalidPayload(f"« {key} » manquant")
    return value


def _require_dict(payload: dict, key: str) -> dict:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise InvalidPayload(f"« {key} » manquant")
    return value
