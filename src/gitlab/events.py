"""Transformation des événements GitLab en notifications neutres (sans discord.py).

Chaque fonction ``format_*`` reçoit le JSON envoyé par GitLab et retourne une
``Notification`` prête à afficher, ou ``None`` si l'événement ne doit pas être
notifié (branche non suivie, action ignorée…). Un JSON incomplet lève
``InvalidPayload`` : le serveur répond alors 400 à GitLab.

Formats de référence : https://docs.gitlab.com/user/project/integrations/webhook_events/
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass

from src.gitlab.rules import GitLabRules
from src.gitlab.security import clean_text, describe, first_line, inline_code, plain_text, safe_url
from src.services import text_limits as limits
from src.services.content import EmbedField
from src.services.design import Color, format_duration, parse_timestamp, short_sha

# En-tête X-Gitlab-Event → type d'événement géré par le bot.
SUPPORTED_EVENTS = {
    "Push Hook": "push",
    "Merge Request Hook": "merge_request",
    "Pipeline Hook": "pipeline",
}

ZERO_SHA = "0" * 40

# Couleurs : voir src/services/design.py (vert = réussite, rouge = échec, orange = avertissement, bleu = info).
COLOR_PUSH = int(Color.INFO)
COLOR_SUCCESS = int(Color.SUCCESS)
COLOR_FAILURE = int(Color.FAILURE)
COLOR_NEUTRAL = int(Color.NEUTRAL)
COLOR_MERGED = int(Color.MERGED)
COLOR_WARNING = int(Color.WARNING)

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

PIPELINE_SOURCES = {
    "push": "un push",
    "merge_request_event": "une merge request",
    "schedule": "une planification",
    "web": "un lancement manuel",
    "api": "l'API",
    "trigger": "un déclencheur",
    "pipeline": "un autre pipeline",
    "parent_pipeline": "un pipeline parent",
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


MR_STATES = {"opened": "ouverte", "merged": "fusionnée", "closed": "fermée", "locked": "verrouillée"}
# Statut de fusion détaillé (detailed_merge_status) : seuls les cas utiles à l'équipe sont traduits.
MR_MERGE_STATUSES = {
    "mergeable": "prête à fusionner",
    "conflict": "conflits à résoudre",
    "not_approved": "approbation requise",
    "ci_must_pass": "pipeline requis",
    "ci_still_running": "pipeline en cours",
    "discussions_not_resolved": "discussions non résolues",
    "draft_status": "brouillon",
    "need_rebase": "rebase nécessaire",
}
MAX_PEOPLE = 5


@dataclass(frozen=True)
class Notification:
    kind: str
    title: str
    description: str
    color: int
    url: str | None = None
    fields: tuple[EmbedField, ...] = ()
    footer: str = ""
    # Données structurées (texte brut, déjà nettoyé) : horodatage de l'embed, historique,
    # /project et déduplication des événements identiques reçus sous des identifiants différents.
    timestamp: str | None = None  # ISO 8601 UTC de l'événement d'après GitLab, si fourni
    dedup_key: str | None = None
    action: str = ""  # push, branch_created, open, merge, success, failed…
    ref: str = ""  # branche concernée
    author: str = ""
    default_branch: str = ""  # branche principale du projet, si GitLab l'indique


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
    branch_txt = inline_code(branch, 100)
    branch_plain = plain_text(branch, 100)

    dedup_key = _dedup("push", project, ref, before, after)
    author_plain = plain_text(payload.get("user_name") or payload.get("user_username"), 100)

    if after == ZERO_SHA:
        return Notification(
            kind="push",
            title=limits.truncate(f"🗑️ Branche supprimée : {branch_plain} · {project.name_plain}", limits.EMBED_TITLE),
            description=f"**{user}** a supprimé la branche {branch_txt}.",
            color=COLOR_NEUTRAL,
            url=project.url,
            footer=project.footer,
            dedup_key=dedup_key,
            action="branch_deleted",
            ref=branch_plain,
            author=author_plain,
            default_branch=project.default_branch,
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
        sha = plain_text(str(commit.get("id") or "")[:8], 20).replace("`", "")
        message = clean_text(first_line(commit.get("title") or commit.get("message")), 120, single_line=True)
        author = commit.get("author") if isinstance(commit.get("author"), dict) else {}
        author_txt = clean_text(author.get("name"), 60, single_line=True)
        commit_url = safe_url(commit.get("url"))
        ref_txt = f"[`{sha}`]({commit_url})" if commit_url and sha else f"`{sha or '?'}`"
        lines.append(f"{ref_txt} {message or '*(sans message)*'}" + (f" — {author_txt}" if author_txt else ""))
    hidden = total - len(shown)
    if hidden > 0:
        lines.append(f"… et {hidden} autre(s) commit(s)")

    intro = f"**{user}** a créé la branche {branch_txt}" if created else f"**{user}** a poussé sur {branch_txt}"
    description = intro + ("\n\n" + "\n".join(lines) if lines else ".")

    url = project.url
    if project.url and not created and before and after:
        url = f"{project.url}/-/compare/{before[:12]}...{after[:12]}"
    elif project.url and created:
        url = f"{project.url}/-/tree/{after[:12]}"

    fields = [EmbedField("Auteur", user, inline=True)]
    files = _file_stats(commits)
    if files:
        detail = "" if total <= len(commits) else f"\n*d'après les {len(commits)} commits détaillés par GitLab*"
        fields.append(EmbedField("Fichiers", files + detail, inline=True))
    latest = commits[-1] if commits and isinstance(commits[-1], dict) else {}

    return Notification(
        kind="push",
        title=limits.truncate(title, limits.EMBED_TITLE),
        description=limits.truncate(description, limits.EMBED_DESCRIPTION),
        color=COLOR_PUSH,
        url=safe_url(url),
        fields=tuple(fields),
        footer=project.footer,
        timestamp=_iso(latest.get("timestamp")),
        dedup_key=dedup_key,
        action="branch_created" if created else "push",
        ref=branch_plain,
        author=author_plain,
        default_branch=project.default_branch,
    )


def _file_stats(commits: list) -> str:
    """Fichiers distincts ajoutés / modifiés / supprimés dans les commits détaillés par GitLab."""
    added: set[str] = set()
    modified: set[str] = set()
    removed: set[str] = set()
    for commit in commits:
        if not isinstance(commit, dict):
            continue
        for key, target in (("added", added), ("modified", modified), ("removed", removed)):
            values = commit.get(key)
            if isinstance(values, list):
                target.update(v for v in values if isinstance(v, str))
    if not (added or modified or removed):
        return ""  # GitLab n'a pas fourni la liste des fichiers : rien à afficher plutôt qu'un faux zéro
    parts = []
    if added:
        parts.append(f"+{len(added)} ajouté(s)")
    if modified:
        parts.append(f"~{len(modified)} modifié(s)")
    if removed:
        parts.append(f"−{len(removed)} supprimé(s)")
    return " · ".join(parts)


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

    source = inline_code(attrs.get("source_branch"), 100)
    target = inline_code(attrs.get("target_branch"), 100)
    fields = [EmbedField("Branches", f"{source} → {target}", inline=True)]
    fields.append(EmbedField("Approuvée par" if action in {"approved", "approval"} else "Par", user_txt, inline=True))
    if draft:
        fields.append(EmbedField("État", "brouillon (draft)", inline=True))
    status = _mr_status(attrs, action)
    if status:
        fields.append(EmbedField("Statut", status, inline=True))
    for key, name in (("reviewers", "Relecteurs"), ("assignees", "Assignés")):
        people = _people(payload.get(key))
        if people:
            fields.append(EmbedField(name, people, inline=True))
    if changed:
        fields.append(EmbedField("Modifié", ", ".join(changed), inline=False))

    description = ""
    if action in {"open", "reopen"}:
        description = clean_text(attrs.get("description"), 300)

    plain_title = plain_text(first_line(attrs.get("title")), 200) or "(sans titre)"
    updated_at = attrs.get("updated_at")
    # Sans date de mise à jour, deux actions identiques légitimes (fermer, rouvrir, refermer) seraient
    # confondues : on ne déduplique alors que par identifiant de livraison.
    dedup_key = None
    if updated_at:
        dedup_key = _dedup("merge_request", project, iid, action, updated_at, user.get("username"), sorted(changed))
    return Notification(
        kind="merge_request",
        title=limits.truncate(f"{icon} MR !{iid} {label} : {plain_title}", limits.EMBED_TITLE),
        description=description or f"**{title}**",
        color=color,
        url=safe_url(attrs.get("url")) or project.url,
        fields=tuple(fields),
        footer=project.footer,
        timestamp=_iso(updated_at),
        dedup_key=dedup_key,
        action=action,
        ref=plain_text(attrs.get("target_branch"), 100),
        author=plain_text(user.get("name") or user.get("username"), 100),
        default_branch=project.default_branch,
    )


def _mr_status(attrs: dict, action: str) -> str:
    """État de la MR et, si GitLab l'indique, ce qui bloque la fusion (seulement pour une MR ouverte)."""
    state = attrs.get("state")
    if action in {"merge", "close"} or not isinstance(state, str):
        return ""  # le titre dit déjà « fusionnée » ou « fermée »
    parts = [MR_STATES.get(state, plain_text(state, 30))]
    merge_status = attrs.get("detailed_merge_status")
    if state == "opened" and isinstance(merge_status, str) and merge_status in MR_MERGE_STATUSES:
        if merge_status != "draft_status":  # déjà affiché dans « État »
            parts.append(MR_MERGE_STATUSES[merge_status])
    return " · ".join(p for p in parts if p)


def _people(value: object) -> str:
    if not isinstance(value, list):
        return ""
    names = [
        clean_text(person.get("name") or person.get("username"), 60, single_line=True)
        for person in value
        if isinstance(person, dict)
    ]
    names = [n for n in names if n]
    if len(names) > MAX_PEOPLE:
        names = [*names[:MAX_PEOPLE], f"+{len(names) - MAX_PEOPLE}"]
    return ", ".join(names)


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

    fields = [EmbedField("Branche", inline_code(ref, 100), inline=True)]
    duration = attrs.get("duration")
    if isinstance(duration, (int, float)) and not isinstance(duration, bool) and duration >= 0:
        fields.append(EmbedField("Durée", format_duration(duration), inline=True))
    user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
    user_txt = clean_text(user.get("name") or user.get("username"), 100, single_line=True)
    if user_txt:
        fields.append(EmbedField("Lancé par", user_txt, inline=True))

    commit = payload.get("commit") if isinstance(payload.get("commit"), dict) else {}
    full_sha = plain_text(str(commit.get("id") or attrs.get("sha") or ""), 64).replace("`", "")
    sha = short_sha(full_sha)
    commit_title = clean_text(first_line(commit.get("title") or commit.get("message")), 150, single_line=True)
    if sha or commit_title:
        commit_url = safe_url(commit.get("url")) or (
            safe_url(f"{project.url}/-/commit/{full_sha}") if project.url and full_sha.isalnum() else None
        )
        ref_txt = f"[`{sha}`]({commit_url})" if commit_url and sha else f"`{sha or '?'}`"
        fields.append(EmbedField("Commit", f"{ref_txt} {commit_title}".strip(), inline=False))
    source = attrs.get("source")
    if isinstance(source, str) and source in PIPELINE_SOURCES:
        fields.append(EmbedField("Déclenché par", PIPELINE_SOURCES[source], inline=True))

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
    moment = attrs.get("finished_at") or attrs.get("updated_at") or attrs.get("created_at")
    return Notification(
        kind="pipeline",
        title=limits.truncate(f"{icon} Pipeline #{pipeline_id} {label} · {project.name_plain}", limits.EMBED_TITLE),
        description="",
        color=color,
        url=url,
        fields=tuple(fields),
        footer=project.footer,
        timestamp=_iso(moment),
        dedup_key=_dedup("pipeline", project, pipeline_id, status, attrs.get("finished_at")),
        action=status,
        ref=plain_text(ref, 100),
        author=plain_text(user.get("name") or user.get("username"), 100),
        default_branch=project.default_branch,
    )


# --- Outils ----------------------------------------------------------------


@dataclass(frozen=True)
class _Project:
    name_plain: str
    url: str | None
    footer: str
    id: int | None = None
    default_branch: str = ""


def _project(payload: dict) -> _Project:
    project = payload.get("project")
    if not isinstance(project, dict):
        raise InvalidPayload("« project » manquant")
    name = plain_text(first_line(project.get("path_with_namespace") or project.get("name")), 100) or "projet GitLab"
    return _Project(
        name_plain=name,
        url=safe_url(project.get("web_url")),
        footer=limits.truncate(f"GitLab · {name}", 200),
        id=project_id_of(payload),
        default_branch=plain_text(project.get("default_branch"), 100),
    )


def _dedup(kind: str, project: _Project, *parts: object) -> str:
    """Empreinte d'un événement : deux livraisons du même fait GitLab donnent la même clé."""
    raw = json.dumps([kind, project.id, *[p if isinstance(p, (str, int, list)) else str(p) for p in parts]])
    return f"{kind}:{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:32]}"


def _iso(value: object) -> str | None:
    moment = parse_timestamp(value)
    return moment.isoformat() if moment else None


def _expect_kind(payload: dict, kind: str) -> None:
    if payload.get("object_kind") != kind:
        raise InvalidPayload(f"object_kind {describe(payload.get('object_kind'))} ne correspond pas à l'en-tête ({kind})")


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
