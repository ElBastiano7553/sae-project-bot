"""Catalogue de l'aide (/help) : une entrée par commande réellement enregistrée.

Un test vérifie que ce catalogue et les commandes slash du bot correspondent
exactement : une commande ajoutée sans aide, ou une aide sans commande, fait
échouer la suite de tests.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class HelpEntry:
    command: str  # nom complet, ex. "admin diagnostic"
    category: str
    description: str
    example: str = ""
    admin: bool = False


CATEGORIES = {
    "consultation": "🔎 Consulter",
    "activite": "📈 Activité GitLab",
    "ia": "🤖 Assistant IA",
    "administration": "🔒 Administrer",
}

HELP_ENTRIES: tuple[HelpEntry, ...] = (
    HelpEntry("help", "consultation", "Affiche cette aide.", "/help"),
    HelpEntry("status", "consultation", "État du bot : Discord, base de données, webhooks GitLab, IA, dernière erreur.", "/status"),
    HelpEntry("health", "consultation", "Ancien nom de /status, conservé pour compatibilité.", "/health"),
    HelpEntry(
        "project",
        "consultation",
        "Résumé du projet : liens configurés, branche principale, derniers événements GitLab reçus.",
        "/project",
    ),
    HelpEntry(
        "events",
        "activite",
        "Historique des événements GitLab reçus, filtrable par type, période et texte (type « erreurs » : admins).",
        "/events type:Pipelines periode:7 derniers jours recherche:main",
    ),
    HelpEntry("stats", "activite", "Commits, merge requests, pipelines et déploiements sur une période, avec l'évolution.", "/stats periode:30 derniers jours"),
    HelpEntry(
        "leaderboard",
        "activite",
        "Contributions : MR fusionnées, approbations, jours actifs (les commits ne comptent pas). Pas de classement sans assez de données.",
        "/leaderboard periode:30 derniers jours details:True",
    ),
    HelpEntry("ask", "ia", "Question sur le projet : l'IA répond à partir des documents de l'équipe et cite ses sources.", "/ask question:Quand est la prochaine soutenance ?"),
    HelpEntry("summary", "ia", "Bilan de l'activité GitLab de la période, rédigé par l'IA à partir des événements reçus.", "/summary periode:7 derniers jours"),
    HelpEntry("review", "ia", "Relecture d'une merge request par l'IA : bugs probables, tests, sécurité (une aide, pas une validation).", "/review mr:12"),
    HelpEntry("setup", "administration", "Prévisualise la structure du serveur et les messages permanents, puis applique après confirmation.", "/setup", True),
    HelpEntry("refresh", "administration", "Met à jour les messages permanents sur place (tous, ou un seul).", "/refresh message:liens", True),
    HelpEntry("config", "administration", "Réglages du bot (sans aucun secret) et salons associés.", "/config", True),
    HelpEntry("announce", "administration", "Rédige ou modifie l'annonce en cours de #annonces (prévisualisation puis confirmation).", "/announce", True),
    HelpEntry("admin messages", "administration", "État de chaque message permanent : lien, dernière action, erreur, liens à compléter.", "/admin messages", True),
    HelpEntry(
        "admin rebuild",
        "administration",
        "Reconstruit un message permanent (nouveau message puis suppression de l'ancien), après confirmation.",
        "/admin rebuild message:bienvenue",
        True,
    ),
    HelpEntry("admin diagnostic", "administration", "Vérifie les permissions du bot dans chaque salon utilisé.", "/admin diagnostic", True),
    HelpEntry("admin history", "administration", "Journal des dernières actions d'administration.", "/admin history", True),
)


def entries_by_category(include_admin: bool = True) -> dict[str, list[HelpEntry]]:
    grouped: dict[str, list[HelpEntry]] = {key: [] for key in CATEGORIES}
    for entry in HELP_ENTRIES:
        if entry.admin and not include_admin:
            continue
        grouped[entry.category].append(entry)
    return {key: items for key, items in grouped.items() if items}
