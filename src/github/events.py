"""Transformation des événements GitHub en notifications neutres (sans discord.py).

Chaque fonction ``format_*`` reçoit le JSON envoyé par GitHub et retourne une
``Notification`` (même objet que pour GitLab, donc même affichage sur Discord),
ou ``None`` si l'événement ne mérite pas de notification. Un JSON incomplet lève
``InvalidPayload`` : le serveur répond alors 400 à GitHub.

Choix pour éviter le bruit et les doublons entre événements :

* push : seules les branches (pas les tags) ; les commits déjà connus du dépôt
  (``distinct: false``, par exemple lors d'une avance rapide de ``main``) ne sont
  pas réannoncés ;
* pull_request : ouverture, réouverture, passage en « prête pour relecture »,
  fusion et fermeture. Les nouveaux commits (``synchronize``) sont déjà annoncés
  par le push, les modifications de labels, relecteurs… sont ignorées ;
* issues : ouverture, fermeture, réouverture ;
* workflow_run : uniquement les exécutions terminées en échec (une réussite ne
  demande aucune action de l'équipe).

Formats de référence : https://docs.github.com/webhooks/webhook-events-and-payloads
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass

from src.gitlab.events import InvalidPayload, Notification
from src.gitlab.security import clean_text, first_line, inline_code, plain_text, safe_url
from src.services import text_limits as limits
from src.services.content import EmbedField
from src.services.design import Color, format_duration, parse_timestamp, short_sha

ZERO_SHA = "0" * 40
MAX_COMMITS = 5
MAX_PEOPLE = 5
MAX_LABELS = 8
BODY_PREVIEW = 300

PR_LABELS = {
    "opened": ("🆕", "ouverte", int(Color.INFO)),
    "reopened": ("🔁", "rouverte", int(Color.INFO)),
    "ready_for_review": ("👀", "prête pour relecture", int(Color.INFO)),
    "closed": ("🚫", "fermée", int(Color.NEUTRAL)),
    "merged": ("🎉", "fusionnée", int(Color.MERGED)),
}

ISSUE_LABELS = {
    "opened": ("🐛", "ouverte", int(Color.INFO)),
    "reopened": ("🔁", "rouverte", int(Color.INFO)),
    "closed": ("✅", "fermée", int(Color.SUCCESS)),
    "closed_not_planned": ("🚫", "fermée (non prévue)", int(Color.NEUTRAL)),
}

# Conclusions d'une exécution GitHub Actions considérées comme un échec.
WORKFLOW_FAILURES = {
    "failure": "en échec",
    "timed_out": "interrompu (délai dépassé)",
    "startup_failure": "impossible à démarrer",
}

WORKFLOW_TRIGGERS = {
    "push": "un push",
    "pull_request": "une pull request",
    "pull_request_target": "une pull request",
    "schedule": "une planification",
    "workflow_dispatch": "un lancement manuel",
    "workflow_run": "un autre workflow",
    "release": "une release",
}


@dataclass(frozen=True)
class _Repository:
    full_name: str
    url: str | None
    footer: str
    id: int | None
    default_branch: str


def repository_full_name(payload: dict) -> str | None:
    """Nom complet du dépôt (``propriétaire/nom``) d'après le JSON, ou ``None`` s'il manque."""
    repository = payload.get("repository")
    name = repository.get("full_name") if isinstance(repository, dict) else None
    return name if isinstance(name, str) and name else None


def format_event(kind: str, payload: dict) -> Notification | None:
    formatter: Callable[[dict], Notification | None] = FORMATTERS[kind]
    return formatter(payload)


# --- Push ------------------------------------------------------------------


def format_push(payload: dict) -> Notification | None:
    repo = _repository(payload)
    ref = _require_str(payload, "ref")
    if not ref.startswith("refs/heads/"):
        return None  # tags : non gérés
    branch = ref.removeprefix("refs/heads/")
    before = str(payload.get("before") or "")
    after = str(payload.get("after") or "")
    commits = payload.get("commits") or []
    if not isinstance(commits, list) or not all(isinstance(commit, dict) for commit in commits):
        raise InvalidPayload("« commits » doit être une liste d'objets")
    pusher = _dict(payload.get("pusher"))
    sender = _dict(payload.get("sender"))
    user_raw = pusher.get("name") or sender.get("login")
    user = clean_text(user_raw, 100, single_line=True) or "Quelqu'un"
    branch_txt = inline_code(branch, 100)
    branch_plain = plain_text(branch, 100)
    common = {
        "kind": "github_push",
        "footer": repo.footer,
        "dedup_key": _dedup("push", repo, ref, before, after),
        "ref": branch_plain,
        "author": plain_text(user_raw, 100),
        "default_branch": repo.default_branch,
    }

    if payload.get("deleted") is True or after == ZERO_SHA:
        return Notification(
            title=limits.truncate(f"🗑️ Branche supprimée : {branch_plain} · {repo.full_name}", limits.EMBED_TITLE),
            description=f"**{user}** a supprimé la branche {branch_txt}.",
            color=int(Color.NEUTRAL),
            url=repo.url,
            action="branch_deleted",
            **common,
        )

    created = payload.get("created") is True or before == ZERO_SHA
    # Commits déjà présents ailleurs dans le dépôt (avance rapide, branche créée depuis main…) : déjà annoncés.
    new_commits = [commit for commit in commits if commit.get("distinct", True) is not False]
    if not new_commits and not created:
        return None

    forced = payload.get("forced") is True
    total = len(new_commits)
    if created and not new_commits:
        title = f"🌱 Nouvelle branche : {branch_plain} · {repo.full_name}"
    else:
        noun = "commit" if total == 1 else "commits"
        title = f"📤 {total} {noun} sur {branch_plain} · {repo.full_name}"

    lines = []
    # GitHub envoie les commits du plus ancien au plus récent : on affiche les plus récents.
    shown = new_commits[-MAX_COMMITS:]
    for commit in reversed(shown):
        sha = plain_text(short_sha(commit.get("id")), 20).replace("`", "")
        message = clean_text(first_line(commit.get("message")), 120, single_line=True)
        author_txt = clean_text(_dict(commit.get("author")).get("name"), 60, single_line=True)
        commit_url = safe_url(commit.get("url"))
        ref_txt = f"[`{sha}`]({commit_url})" if commit_url and sha else f"`{sha or '?'}`"
        lines.append(f"{ref_txt} {message or '*(sans message)*'}" + (f" — {author_txt}" if author_txt else ""))
    hidden = total - len(shown)
    if hidden > 0:
        lines.append(f"… et {hidden} autre(s) commit(s)")

    if created:
        intro = f"**{user}** a créé la branche {branch_txt}"
    else:
        intro = f"**{user}** a {'forcé un push' if forced else 'poussé'} sur {branch_txt}"
    description = intro + ("\n\n" + "\n".join(lines) if lines else ".")

    fields = [EmbedField("Auteur", user, inline=True)]
    files = _file_stats(new_commits)
    if files:
        fields.append(EmbedField("Fichiers", files, inline=True))
    if forced:
        fields.append(EmbedField("Attention", "historique réécrit (push forcé)", inline=True))
    latest = new_commits[-1] if new_commits else {}

    return Notification(
        title=limits.truncate(title, limits.EMBED_TITLE),
        description=limits.truncate(description, limits.EMBED_DESCRIPTION),
        color=int(Color.WARNING) if forced else int(Color.INFO),
        url=safe_url(payload.get("compare")) or repo.url,
        fields=tuple(fields),
        timestamp=_iso(latest.get("timestamp")),
        action="branch_created" if created else "push",
        commit_count=total,
        **common,
    )


def _file_stats(commits: list[dict]) -> str:
    """Fichiers distincts ajoutés / modifiés / supprimés dans les commits annoncés."""
    added: set[str] = set()
    modified: set[str] = set()
    removed: set[str] = set()
    for commit in commits:
        for key, target in (("added", added), ("modified", modified), ("removed", removed)):
            values = commit.get(key)
            if isinstance(values, list):
                target.update(v for v in values if isinstance(v, str))
    parts = []
    if added:
        parts.append(f"+{len(added)} ajouté(s)")
    if modified:
        parts.append(f"~{len(modified)} modifié(s)")
    if removed:
        parts.append(f"−{len(removed)} supprimé(s)")
    return " · ".join(parts)


# --- Pull requests -----------------------------------------------------------


def format_pull_request(payload: dict) -> Notification | None:
    action = payload.get("action")
    if not isinstance(action, str) or action not in PR_LABELS or action == "merged":
        return None
    repo = _repository(payload)
    pr = _require_dict(payload, "pull_request")
    number = _require_int(pr, "number", "pull_request.number")
    merged = action == "closed" and pr.get("merged") is True
    icon, label, color = PR_LABELS["merged" if merged else action]
    title_txt = plain_text(pr.get("title"), 200) or "(sans titre)"
    sender = _dict(payload.get("sender"))
    user_txt = clean_text(sender.get("login"), 100, single_line=True) or "Quelqu'un"
    head = inline_code(_dict(pr.get("head")).get("ref"), 100)
    base = inline_code(_dict(pr.get("base")).get("ref"), 100)

    fields = [
        EmbedField("Branches", f"{head} → {base}", inline=True),
        EmbedField("Fusionnée par" if merged else "Par", user_txt, inline=True),
    ]
    if pr.get("draft") is True:
        fields.append(EmbedField("État", "brouillon (draft)", inline=True))
    size = _pr_size(pr)
    if size:
        fields.append(EmbedField("Taille", size, inline=True))
    for key, name in (("requested_reviewers", "Relecteurs demandés"), ("assignees", "Assignés")):
        people = _people(pr.get(key))
        if people:
            fields.append(EmbedField(name, people, inline=True))

    description = ""
    if action in {"opened", "ready_for_review"}:
        description = clean_text(pr.get("body"), BODY_PREVIEW)
    moment = pr.get("merged_at") if merged else (pr.get("closed_at") if action == "closed" else pr.get("updated_at"))
    return Notification(
        kind="github_pull_request",
        title=limits.truncate(f"{icon} PR #{number} {label} : {title_txt} · {repo.full_name}", limits.EMBED_TITLE),
        description=description,
        color=color,
        url=safe_url(pr.get("html_url")) or repo.url,
        fields=tuple(fields),
        footer=repo.footer,
        timestamp=_iso(moment),
        dedup_key=_dedup("pull_request", repo, number, action, merged, pr.get("updated_at")),
        action="merge" if merged else action,
        ref=plain_text(_dict(pr.get("head")).get("ref"), 100),
        author=plain_text(sender.get("login"), 100),
        default_branch=repo.default_branch,
        subject_id=number,
    )


def _pr_size(pr: dict) -> str:
    parts = []
    commits, additions, deletions, files = (_int(pr.get(key)) for key in ("commits", "additions", "deletions", "changed_files"))
    if commits is not None:
        parts.append(f"{commits} commit(s)")
    if files is not None:
        parts.append(f"{files} fichier(s)")
    if additions is not None and deletions is not None:
        parts.append(f"+{additions} / −{deletions}")
    return " · ".join(parts)


# --- Issues --------------------------------------------------------------------


def format_issue(payload: dict) -> Notification | None:
    action = payload.get("action")
    if action not in {"opened", "closed", "reopened"}:
        return None
    repo = _repository(payload)
    issue = _require_dict(payload, "issue")
    number = _require_int(issue, "number", "issue.number")
    key = "closed_not_planned" if action == "closed" and issue.get("state_reason") == "not_planned" else action
    icon, label, color = ISSUE_LABELS[key]
    title_txt = plain_text(issue.get("title"), 200) or "(sans titre)"
    sender = _dict(payload.get("sender"))
    user_txt = clean_text(sender.get("login"), 100, single_line=True) or "Quelqu'un"

    fields = [EmbedField("Par", user_txt, inline=True)]
    labels = issue.get("labels")
    if isinstance(labels, list):
        names = [clean_text(_dict(item).get("name"), 50, single_line=True) for item in labels[:MAX_LABELS]]
        names = [name for name in names if name]
        if names:
            fields.append(EmbedField("Labels", ", ".join(names), inline=True))
    people = _people(issue.get("assignees"))
    if people:
        fields.append(EmbedField("Assignés", people, inline=True))

    moment = issue.get("closed_at") if action == "closed" else issue.get("updated_at")
    return Notification(
        kind="github_issue",
        title=limits.truncate(f"{icon} Issue #{number} {label} : {title_txt} · {repo.full_name}", limits.EMBED_TITLE),
        description=clean_text(issue.get("body"), BODY_PREVIEW) if action == "opened" else "",
        color=color,
        url=safe_url(issue.get("html_url")) or repo.url,
        fields=tuple(fields),
        footer=repo.footer,
        timestamp=_iso(moment),
        dedup_key=_dedup("issues", repo, number, action, issue.get("updated_at")),
        action=action,
        author=plain_text(sender.get("login"), 100),
        default_branch=repo.default_branch,
        subject_id=number,
    )


# --- GitHub Actions ------------------------------------------------------------


def format_workflow_run(payload: dict) -> Notification | None:
    if payload.get("action") != "completed":
        return None
    repo = _repository(payload)
    run = _require_dict(payload, "workflow_run")
    conclusion = run.get("conclusion")
    if conclusion not in WORKFLOW_FAILURES:
        return None  # réussite, annulation, ignoré… : rien à signaler
    run_id = _require_int(run, "id", "workflow_run.id")
    attempt = _int(run.get("run_attempt")) or 1
    name = plain_text(run.get("name"), 100) or "workflow"
    branch = plain_text(run.get("head_branch"), 100)
    where = f" sur {branch}" if branch else ""

    fields = []
    if branch:
        fields.append(EmbedField("Branche", inline_code(branch, 100), inline=True))
    actor = _dict(run.get("triggering_actor")) or _dict(run.get("actor"))
    actor_txt = clean_text(actor.get("login"), 100, single_line=True)
    if actor_txt:
        fields.append(EmbedField("Déclenché par", actor_txt, inline=True))
    trigger = run.get("event")
    if isinstance(trigger, str) and trigger:
        fields.append(EmbedField("Origine", WORKFLOW_TRIGGERS.get(trigger, clean_text(trigger, 50, single_line=True)), inline=True))
    if attempt > 1:
        fields.append(EmbedField("Tentative", str(attempt), inline=True))
    started, ended = parse_timestamp(run.get("run_started_at")), parse_timestamp(run.get("updated_at"))
    if started and ended and ended >= started:
        fields.append(EmbedField("Durée", format_duration((ended - started).total_seconds()), inline=True))
    sha = plain_text(short_sha(run.get("head_sha")), 20).replace("`", "")
    commit_title = clean_text(first_line(run.get("display_title") or _dict(run.get("head_commit")).get("message")), 150, single_line=True)
    if sha or commit_title:
        fields.append(EmbedField("Commit", f"`{sha or '?'}` {commit_title}".strip(), inline=False))

    number = _int(run.get("run_number"))
    label = WORKFLOW_FAILURES[conclusion]
    return Notification(
        kind="github_workflow",
        title=limits.truncate(f"❌ Workflow « {name} » {label}{where} · {repo.full_name}", limits.EMBED_TITLE),
        description=f"Exécution n° {number}." if number is not None else "",
        color=int(Color.FAILURE),
        url=safe_url(run.get("html_url")) or repo.url,
        fields=tuple(fields),
        footer=repo.footer,
        timestamp=_iso(run.get("updated_at")),
        dedup_key=_dedup("workflow_run", repo, run_id, attempt),
        action=conclusion,
        ref=branch,
        author=plain_text(actor.get("login"), 100),
        default_branch=repo.default_branch,
        subject_id=run_id,
    )


# En-tête X-GitHub-Event → mise en forme. « ping » est traité directement par le serveur.
FORMATTERS: dict[str, Callable[[dict], Notification | None]] = {
    "push": format_push,
    "pull_request": format_pull_request,
    "issues": format_issue,
    "workflow_run": format_workflow_run,
}
SUPPORTED_EVENTS = frozenset(FORMATTERS)


# --- Outils ----------------------------------------------------------------


def _repository(payload: dict) -> _Repository:
    repository = _require_dict(payload, "repository")
    name = plain_text(repository.get("full_name"), 100) or "dépôt GitHub"
    return _Repository(
        full_name=name,
        url=safe_url(repository.get("html_url")),
        footer=limits.truncate(f"GitHub · {name}", 200),
        id=_int(repository.get("id")),
        default_branch=plain_text(repository.get("default_branch"), 100),
    )


def _people(value: object) -> str:
    if not isinstance(value, list):
        return ""
    names = [clean_text(_dict(person).get("login"), 50, single_line=True) for person in value[:MAX_PEOPLE]]
    names = [name for name in names if name]
    extra = len(value) - MAX_PEOPLE
    return ", ".join(names) + (f" (+{extra})" if extra > 0 and names else "")


def _dedup(kind: str, repo: _Repository, *parts: object) -> str:
    """Empreinte d'un fait GitHub : deux livraisons du même fait donnent la même clé."""
    raw = json.dumps([kind, repo.id, repo.full_name, *[p if isinstance(p, (str, int, bool)) or p is None else str(p) for p in parts]])
    return f"github:{kind}:{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:32]}"


def _dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _iso(value: object) -> str | None:
    moment = parse_timestamp(value)
    return moment.isoformat() if moment else None


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


def _require_int(table: dict, key: str, label: str) -> int:
    value = _int(table.get(key))
    if value is None:
        raise InvalidPayload(f"« {label} » manquant")
    return value
