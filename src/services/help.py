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
    "reunions": "🗓️ Réunions",
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
    HelpEntry(
        "reunion creer",
        "reunions",
        "Crée une réunion (titre, objectifs, ordre du jour séparé par « ; », responsable, participants, durée) et publie son panneau.",
        "/reunion creer titre:Point sprint 3 etapes:Bilan ; Démo ; Planning participants:@Bob @Chloé duree:45",
    ),
    HelpEntry("reunion voir", "reunions", "Affiche une réunion ; `publier:True` déplace son panneau dans ce salon (responsable).", "/reunion voir reunion:3"),
    HelpEntry("reunion liste", "reunions", "Liste les réunions prévues, en cours, terminées ou annulées.", "/reunion liste statut:Terminées"),
    HelpEntry("reunion rejoindre", "reunions", "Rejoint une réunion prévue ou en cours (aussi : bouton « Rejoindre »).", "/reunion rejoindre"),
    HelpEntry("reunion quitter", "reunions", "Quitte une réunion (le responsable ne peut pas quitter la sienne).", "/reunion quitter"),
    HelpEntry("reunion inviter", "reunions", "Ajoute un participant (responsable ou admin).", "/reunion inviter membre:@Bob"),
    HelpEntry("reunion demarrer", "reunions", "Démarre la réunion et sa première étape (responsable ou admin).", "/reunion demarrer"),
    HelpEntry("reunion suivante", "reunions", "Termine l'étape en cours et passe à la suivante (responsable ou admin).", "/reunion suivante"),
    HelpEntry("reunion precedente", "reunions", "Revient à l'étape précédente (responsable ou admin).", "/reunion precedente"),
    HelpEntry("reunion progression", "reunions", "Étapes traitées, étape en cours, temps écoulé par rapport à la durée prévue.", "/reunion progression"),
    HelpEntry("reunion point", "reunions", "Ajoute un point à la fin de l'ordre du jour (participants).", "/reunion point texte:Choix du moteur audio"),
    HelpEntry("reunion decision", "reunions", "Enregistre une décision, avec un motif facultatif (responsable ou admin, réunion en cours).", "/reunion decision texte:On garde Pygame motif:déjà maîtrisé"),
    HelpEntry("reunion reserve", "reunions", "Enregistre une réserve ou un désaccord, conservé dans le compte rendu (participants).", "/reunion reserve texte:Risque sur les délais type:Désaccord"),
    HelpEntry("reunion action", "reunions", "Ajoute une action à effectuer avec son responsable et une échéance facultative.", "/reunion action description:Écrire le README responsable:@Bob echeance:05/11"),
    HelpEntry("reunion terminer", "reunions", "Termine la réunion après confirmation, fige et publie le compte rendu (responsable ou admin).", "/reunion terminer"),
    HelpEntry("reunion annuler", "reunions", "Annule une réunion prévue ou en cours, après confirmation ; l'historique est conservé.", "/reunion annuler reunion:4"),
    HelpEntry("reunion compte-rendu", "reunions", "Compte rendu paginé (aperçu provisoire si la réunion n'est pas terminée).", "/reunion compte-rendu reunion:3"),
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
    HelpEntry("admin backup", "administration", "Sauvegarde immédiate et vérifiée de la base de données (rotation automatique).", "/admin backup", True),
    HelpEntry("admin history", "administration", "Journal des dernières actions d'administration.", "/admin history", True),
)


def entries_by_category(include_admin: bool = True) -> dict[str, list[HelpEntry]]:
    grouped: dict[str, list[HelpEntry]] = {key: [] for key in CATEGORIES}
    for entry in HELP_ENTRIES:
        if entry.admin and not include_admin:
            continue
        grouped[entry.category].append(entry)
    return {key: items for key, items in grouped.items() if items}
