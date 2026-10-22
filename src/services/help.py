"""Registre de l'aide (/help, /reunion help), sans dépendance à discord.py.

Deux sources, sans duplication :

* **Discord (le code des commandes)** : nom, description courte et paramètres
  (obligatoires ou facultatifs) sont lus dans l'arbre des commandes enregistrées
  (``discord_bot.help_ui.collect_commands``) au moment d'afficher l'aide ;
* **ce registre** : ce qui ne se déduit pas du code, c'est-à-dire la catégorie,
  l'explication pédagogique, un exemple et les pages de guide.

Des tests vérifient que ce registre ne devient pas obsolète en silence : chaque
commande enregistrée a une entrée (et inversement), chaque exemple utilise une
commande et des paramètres qui existent (obligatoires compris), et chaque
``/commande`` citée dans les pages de guide existe vraiment.

Documenter une nouvelle commande : ajouter une ligne ``HelpEntry`` ci-dessous
(catégorie, explication, exemple) ; ses paramètres s'affichent tout seuls.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class HelpEntry:
    command: str  # nom complet, ex. "admin diagnostic"
    category: str
    description: str
    example: str = ""
    admin: bool = False


CATEGORIES = {
    "consultation": "Consulter",
    "activite": "Activité GitLab",
    "ia": "Assistant IA",
    "reunions": "Réunions",
    "administration": "Administrer",
}

HELP_ENTRIES: tuple[HelpEntry, ...] = (
    HelpEntry("help", "consultation", "Centre d'aide paginé : présentation, réunions, commandes, organisation, dépannage.", "/help section:Dépannage"),
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
    HelpEntry("reunion help", "reunions", "Guide pas à pas des réunions : créer, participer, animer, décisions, compte rendu, dépannage.", "/reunion help sujet:Terminer"),
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
    HelpEntry(
        "refonte",
        "administration",
        "Analyse le serveur et propose un plan (créer, renommer, déplacer, sans jamais supprimer), appliqué après confirmation.",
        "/refonte facultatifs:True",
        True,
    ),
)

# Qui peut utiliser chaque commande de réunion (règles appliquées par services.meetings).
MEETING_CONTROL = {"inviter", "demarrer", "suivante", "precedente", "decision", "terminer", "annuler"}
MEETING_CONTRIBUTION = {"point", "reserve", "action"}


def permission_label(entry: HelpEntry) -> str:
    if entry.admin:
        return "administrateurs du bot"
    parts = entry.command.split()
    if parts[0] == "reunion" and len(parts) > 1:
        if parts[1] in MEETING_CONTROL:
            return "responsable de la réunion ou administrateur"
        if parts[1] in MEETING_CONTRIBUTION:
            return "participants de la réunion"
    return "tous les membres"



def entry_for(command: str) -> HelpEntry | None:
    return next((e for e in HELP_ENTRIES if e.command == command), None)


# --- Commandes réelles (lues dans discord.py par discord_bot.help_ui) -----------------------


@dataclass(frozen=True)
class ParamInfo:
    name: str
    description: str
    required: bool


@dataclass(frozen=True)
class CommandInfo:
    name: str  # nom complet, ex. "reunion creer"
    description: str
    params: tuple[ParamInfo, ...] = ()
    admin: bool = False  # masquée par défaut (« Gérer le serveur »)

    @property
    def required(self) -> list[str]:
        return [p.name for p in self.params if p.required]

    @property
    def optional(self) -> list[str]:
        return [p.name for p in self.params if not p.required]


def command_lines(commands: Iterable[CommandInfo], *, include_admin: bool) -> list[tuple[str, list[str]]]:
    """Page « Commandes » : description, paramètres, exemple et permission de chaque commande, par catégorie."""
    by_name = {c.name: c for c in commands}
    grouped: dict[str, list[str]] = {key: [] for key in CATEGORIES}
    for entry in HELP_ENTRIES:
        info = by_name.get(entry.command)
        if info is None or (entry.admin and not include_admin):
            continue
        lines = [f"**`/{info.name}`** — {entry.description or info.description}"]
        details = []
        if info.required:
            details.append("obligatoire : " + ", ".join(f"`{n}`" for n in info.required))
        if info.optional:
            details.append("facultatif : " + ", ".join(f"`{n}`" for n in info.optional))
        details.append(f"accès : {permission_label(entry)}")
        lines.append("  " + " · ".join(details))
        if entry.example and entry.example != f"/{entry.command}":
            lines.append(f"  ex. `{entry.example}`")
        grouped[entry.category].append("\n".join(lines))
    return [(CATEGORIES[key], items) for key, items in grouped.items() if items]


# --- Vérification des exemples --------------------------------------------------------------

_OPTION = re.compile(r"(?:^|\s)([a-z_-]+):")


def check_example(example: str, commands: Iterable[CommandInfo]) -> list[str]:
    """Problèmes d'un exemple ``/commande option:valeur …`` (liste vide : exemple valide)."""
    if not example.startswith("/"):
        return [f"« {example} » ne commence pas par /"]
    by_name = {c.name: c for c in commands}
    words = example[1:].split()
    info = None
    rest = ""
    for size in (2, 1):
        name = " ".join(words[:size])
        if name in by_name:
            info = by_name[name]
            rest = example[1 + len(name):]
            break
    if info is None:
        return [f"« {example} » : commande inconnue"]
    used = _OPTION.findall(rest)
    known = {p.name for p in info.params}
    problems = [f"« {example} » : option « {name} » inconnue" for name in used if name not in known]
    problems += [f"« {example} » : option obligatoire « {name} » absente" for name in info.required if name not in used]
    return problems


# Une commande citée dans un texte d'aide : « /status », « /reunion creer » (pas « config/server.toml »).
_MENTIONED = re.compile(r"(?:^|(?<=[\s`(«*]))/([a-z][a-z-]*)(?: ([a-z][a-z-]*))?")


def mentioned_commands(text: str, group_names: Iterable[str]) -> set[str]:
    """Commandes citées dans un texte : « reunion creer » pour un groupe, « status » sinon."""
    groups = set(group_names)
    found = set()
    for first, second in _MENTIONED.findall(text):
        found.add(f"{first} {second}" if first in groups and second else first)
    return found


# --- Pages de guide (texte pédagogique, centralisé ici) ---------------------------------------


@dataclass(frozen=True)
class HelpSection:
    title: str
    body: str
    audience: str = "all"  # "all", "member" (non-administrateurs) ou "admin"


@dataclass(frozen=True)
class HelpPage:
    key: str
    title: str
    summary: str  # une ligne, affichée dans le menu déroulant (100 caractères au plus)
    intro: str = ""
    sections: tuple[HelpSection, ...] = ()
    generated: str | None = None  # "commands" : liste construite depuis les commandes réelles


def visible_sections(page: HelpPage, *, admin: bool) -> list[HelpSection]:
    allowed = {"all", "admin" if admin else "member"}
    return [section for section in page.sections if section.audience in allowed]


GUIDE_PAGES: tuple[HelpPage, ...] = (
    HelpPage(
        "accueil",
        "Accueil",
        "Ce que fait Samae et comment utiliser cette aide",
        "Samae est l'assistant de l'équipe : il anime les réunions, suit l'activité GitLab, tient à jour les "
        "messages importants du serveur et répond aux questions sur le projet. Les réponses de l'aide ne sont "
        "visibles que par vous.",
        (
            HelpSection(
                "Ce que Samae fait aujourd'hui",
                "• **Réunions** : ordre du jour, décisions officielles, réserves, actions et compte rendu (`/reunion`).\n"
                "• **Activité GitLab** : notifications des pushs, merge requests et pipelines si les webhooks sont "
                "configurés ; historique `/events`, statistiques `/stats`, contributions `/leaderboard`.\n"
                "• **Assistant IA**, s'il est activé : `/ask`, `/summary`, `/review`.\n"
                "• **Messages permanents** : bienvenue, règles, planning, liens, tableau de bord.\n"
                "• **État** : `/status` pour le bot, `/project` pour les liens et l'activité récente.",
            ),
            HelpSection(
                "Pour commencer",
                "• `/reunion help` : le guide des réunions, pas à pas.\n"
                "• `/project` : liens du projet (dépôt, sujet, documents).\n"
                "• `/status` : vérifier que Samae et ses intégrations fonctionnent.",
            ),
            HelpSection(
                "Naviguer",
                "Boutons **Précédent**, **Suivant** et **Accueil**, ou le menu pour aller directement à une section. "
                "`/help section:Commandes` ouvre directement une section.",
            ),
        ),
    ),
    HelpPage(
        "reunions",
        "Réunions",
        "Organiser une réunion, de l'ordre du jour au compte rendu",
        "Une réunion a un responsable, des participants et un ordre du jour en étapes. Elle produit un compte "
        "rendu figé à la clôture. Tout est enregistré : un redémarrage du bot ne perd rien.",
        (
            HelpSection(
                "En cinq étapes",
                "1. `/reunion creer titre:… etapes:Bilan ; Démo ; Planning` publie le panneau dans le salon.\n"
                "2. Les participants cliquent sur **Rejoindre**.\n"
                "3. Le responsable clique sur **Démarrer**, puis **Étape suivante** au fil de la réunion.\n"
                "4. Pendant la réunion : `/reunion decision` (responsable), `/reunion reserve`, `/reunion action`, "
                "`/reunion point` (participants).\n"
                "5. **Terminer** : le compte rendu est figé puis publié dans le salon des comptes rendus.",
            ),
            HelpSection(
                "Guide complet",
                "`/reunion help` détaille chaque étape avec des exemples vérifiés, les permissions et les solutions "
                "aux problèmes courants.",
            ),
        ),
    ),
    HelpPage("commandes", "Commandes", "Toutes les commandes, leurs options et un exemple", generated="commands"),
    HelpPage(
        "organisation",
        "Organisation",
        "Rôles, idées, votes, décisions, tâches et ressources : ce qui existe",
        "Ce qui est disponible aujourd'hui, et ce qui est seulement prévu.",
        (
            HelpSection(
                "Disponible",
                "• **Décisions officielles** : enregistrées pendant une réunion par le responsable ou un "
                "administrateur (`/reunion decision`), conservées dans le compte rendu.\n"
                "• **Actions** : décidées en réunion, avec un responsable et une échéance (`/reunion action`).\n"
                "• **Réunions** : voir la page Réunions et `/reunion help`.\n"
                "• **Suivi du code** : `/events`, `/stats`, `/leaderboard` et le tableau de bord GitLab.\n"
                "• **Ressources** : les liens du projet sont dans le message « Liens utiles » et `/project`.\n"
                "• **Rôles de projet** (Responsable projet, Game Design, Développement, Art & Design, Testeur) : "
                "ils indiquent qui fait quoi et ne donnent aucun droit ; un administrateur les crée et les attribue.",
            ),
            HelpSection(
                "Pas encore disponible",
                "• **Idées et tier lists** (propositions, votes secrets, classements) : prévues, pas encore "
                "développées. En attendant, utilisez le salon des idées.\n"
                "• **Votes de départage, sondages, catalogue de ressources** : prévus.\n"
                "• **Choisir soi-même ses rôles** (auto-attribution) : prévu.\n"
                "• **Proposer une décision** en réunion pour validation par le responsable : prévu ; aujourd'hui, "
                "seuls le responsable et les administrateurs enregistrent une décision.\n"
                "• **Suivi des tâches hors réunion** (marquer une action comme faite) : pas encore ; les tâches "
                "officielles restent dans les Issues GitLab.",
            ),
        ),
    ),
    HelpPage(
        "administration",
        "Administration",
        "Commandes réservées, permissions, configuration et refonte",
        "",
        (
            HelpSection(
                "Réservé aux administrateurs",
                "Certaines commandes sont réservées aux administrateurs du bot : réorganisation du serveur, "
                "messages permanents, sauvegardes, diagnostic. Elles n'apparaissent pas pour vous. Pour un nouveau "
                "salon, un rôle ou une correction du serveur, demandez à un administrateur.",
                audience="member",
            ),
            HelpSection(
                "Qui est administrateur du bot ?",
                "Toute personne avec la permission « Gérer le serveur » (ou « Administrateur »), ou le rôle défini "
                "dans `ADMIN_ROLE_NAME`. Samae le revérifie à chaque commande et à chaque clic.",
            ),
            HelpSection(
                "Commandes",
                "• `/setup` : retrouve les salons existants et publie les messages permanents (ne crée rien).\n"
                "• `/refresh`, `/announce` : messages permanents et annonce en cours.\n"
                "• `/config`, `/admin diagnostic`, `/admin messages`, `/admin history` : état, permissions, journal.\n"
                "• `/admin backup` : sauvegarde immédiate de la base. `/admin rebuild` : reconstruit un message.\n"
                "• `/refonte` : réorganise le serveur selon la structure cible.",
                audience="admin",
            ),
            HelpSection(
                "Refonte du serveur",
                "1. `/refonte` analyse le serveur sans rien modifier.\n"
                "2. **Prévisualiser** : plan paginé (créations, renommages, déplacements, configuration, "
                "interventions manuelles, risques).\n"
                "3. **Appliquer la refonte** puis **Confirmer** : Samae revérifie vos droits, les siens et l'état "
                "du serveur, sauvegarde sa base, puis applique.\n"
                "4. Rapport : réussies, ignorées, échouées, à faire à la main.\n"
                "Aucune suppression, jamais. Options : `facultatifs:True` (salons facultatifs), `roles:True` "
                "(rôles de projet sans permission). Relancer la commande sur un serveur conforme ne change rien.",
                audience="admin",
            ),
            HelpSection(
                "Permissions nécessaires",
                "• Vous : administrateur du bot, plus « Gérer les salons » pour `/refonte` (et « Gérer les rôles » "
                "avec `roles:True`).\n"
                "• Le bot : « Gérer les salons » pour la refonte, « Gérer les rôles » pour créer des rôles ; "
                "« Voir les salons », « Envoyer des messages », « Intégrer des liens », « Voir les anciens messages » "
                "et « Épingler » dans les salons qu'il utilise (`/admin diagnostic`).",
                audience="admin",
            ),
            HelpSection(
                "Configuration et précautions",
                "La structure cible est dans `config/server.toml` : une clé par fonction, les anciens noms dans "
                "`aliases`, `optional` pour les salons facultatifs. Samae retient les identifiants Discord : un salon "
                "renommé à la main reste reconnu. Chaque refonte laisse une sauvegarde de la base (`data/backups`) et "
                "un journal (`data/refonte`). Une création ne s'annule qu'à la main.",
                audience="admin",
            ),
        ),
    ),
    HelpPage(
        "depannage",
        "Dépannage",
        "Solutions aux problèmes courants",
        "",
        (
            HelpSection(
                "Le bot ne répond pas",
                "Essayez `/status`. Sans réponse du tout, Samae est arrêté ou déconnecté : prévenez la personne qui "
                "l'héberge (le détail est dans `logs/bot.log`).",
            ),
            HelpSection(
                "Une commande n'apparaît pas",
                "Les commandes sont synchronisées au démarrage du bot. Rechargez Discord (Ctrl+R sur ordinateur, "
                "ou fermez puis rouvrez l'application). Les commandes d'administration ne s'affichent que pour les "
                "administrateurs du bot.",
            ),
            HelpSection(
                "Un bouton ne fonctionne plus, ou l'interaction a expiré",
                "Les boutons des panneaux de réunion fonctionnent même après un redémarrage. Les autres boutons "
                "expirent : 10 minutes pour l'aide et la refonte, 2 minutes pour une confirmation. Relancez la "
                "commande. « Panneau périmé » signifie que la réunion a changé : le panneau vient d'être actualisé, "
                "recliquez.",
            ),
            HelpSection(
                "Panneau de réunion introuvable",
                "`/reunion liste`, puis `/reunion voir reunion:<numéro>`. Le responsable peut le republier dans le "
                "salon courant avec `/reunion voir publier:True`. Un panneau supprimé est aussi republié à la "
                "prochaine action sur une réunion ouverte.",
            ),
            HelpSection(
                "Compte rendu non publié",
                "Il reste lisible avec `/reunion compte-rendu reunion:<numéro>`. Le responsable peut réessayer avec "
                "`publier:True` une fois que le bot peut écrire dans le salon des comptes rendus.",
            ),
            HelpSection(
                "Permission manquante",
                "« Le bot n'a pas la permission nécessaire » : le rôle de Samae n'a pas le droit d'agir dans ce "
                "salon. Prévenez un administrateur.",
                audience="member",
            ),
            HelpSection(
                "Permission manquante",
                "`/admin diagnostic` liste, salon par salon, les permissions qui manquent au bot. Corrigez-les dans "
                "les paramètres du salon ou du rôle du bot.",
                audience="admin",
            ),
            HelpSection(
                "Une refonte échoue",
                "Lisez le rapport : chaque échec a sa raison. « Permission refusée » : rôle du bot trop bas ou "
                "permission retirée. « Délai dépassé » : l'opération a pu aboutir. Dans tous les cas, relancez "
                "`/refonte` : elle ne refait que ce qui manque encore.",
                audience="admin",
            ),
        ),
    ),
)

EXAMPLE_CREATE = "/reunion creer titre:Point sprint 3 etapes:Bilan ; Démo ; Planning participants:@Bob @Chloé duree:45"

MEETING_PAGES: tuple[HelpPage, ...] = (
    HelpPage(
        "creer",
        "Créer une réunion",
        "Commande, paramètres, ordre du jour, participants, durée",
        "`/reunion creer` publie le **panneau** de la réunion dans le salon où vous la lancez (de préférence "
        "le salon des réunions). Le panneau est mis à jour à chaque action.",
        (
            HelpSection("Obligatoire", "`titre` : 3 à 100 caractères."),
            HelpSection(
                "Facultatif",
                "• `etapes` : l'ordre du jour, étapes séparées par « ; » (25 au plus).\n"
                "• `participants` : mentionnez-les avec @ dans le texte ; vous pourrez en ajouter ensuite.\n"
                "• `responsable` : vous par défaut ; il fait partie des participants.\n"
                "• `duree` : en minutes, de 5 à 600.\n"
                "• `objectifs`, `description` : textes libres.",
            ),
            HelpSection("Exemple", f"`{EXAMPLE_CREATE}`"),
            HelpSection("Bon à savoir", "Chaque membre peut créer 5 réunions par heure. Le numéro de la réunion (#3…) sert à la désigner ensuite."),
        ),
    ),
    HelpPage(
        "participer",
        "Participer",
        "Voir les réunions, rejoindre, quitter, suivre la progression",
        "",
        (
            HelpSection("Voir les réunions", "`/reunion liste` (prévues ou en cours ; option `statut` pour les autres), puis `/reunion voir reunion:3` pour afficher un panneau en privé."),
            HelpSection(
                "Rejoindre ou quitter",
                "Boutons **Rejoindre** et **Quitter** du panneau, ou `/reunion rejoindre` et `/reunion quitter`. "
                "Le responsable ne peut pas quitter sa propre réunion.",
            ),
            HelpSection("Suivre la progression", "`/reunion progression` : étapes traitées, étape en cours, temps écoulé par rapport à la durée prévue."),
            HelpSection("Identifier le responsable", "Il est indiqué sur le panneau, dans le champ « Responsable »."),
            HelpSection(
                "L'option reunion",
                "Facultative partout : sans elle, Samae prend la réunion ouverte de ce salon (ou la vôtre). S'il y en "
                "a plusieurs, il vous demande de préciser le numéro ; Discord propose la liste pendant la saisie.",
            ),
        ),
    ),
    HelpPage(
        "animer",
        "Animer",
        "Démarrer, changer d'étape, gérer les participants",
        "Réservé au **responsable** de la réunion et aux **administrateurs du bot**.",
        (
            HelpSection("Démarrer", "Bouton **Démarrer** ou `/reunion demarrer` : la réunion passe « en cours » et la première étape commence."),
            HelpSection(
                "Changer d'étape",
                "Boutons **Étape suivante** et **Étape précédente**, ou `/reunion suivante` et `/reunion precedente`. "
                "L'étape quittée est marquée traitée.",
            ),
            HelpSection("Gérer les participants", "`/reunion inviter membre:@Bob` ajoute un participant. Chacun peut aussi rejoindre ou quitter seul."),
            HelpSection(
                "Panneau périmé",
                "Si quelqu'un a modifié la réunion juste avant votre clic, le clic est refusé et le panneau "
                "actualisé : on ne saute jamais deux étapes par erreur. Recliquez.",
            ),
        ),
    ),
    HelpPage(
        "informations",
        "Décisions et actions",
        "Notes, décisions officielles, réserves, actions et échéances",
        "Pendant une réunion **en cours**. Chaque élément est rattaché à l'étape en cours et repris dans le compte rendu.",
        (
            HelpSection(
                "Décision officielle",
                "`/reunion decision texte:On garde Pygame motif:déjà maîtrisé`\n"
                "Réservée au responsable et aux administrateurs : c'est la décision adoptée par l'équipe.",
            ),
            HelpSection(
                "Proposer une décision",
                "Il n'existe **pas encore** de proposition de décision dans Samae (prévu : proposition par un "
                "participant, validation par le responsable). Aujourd'hui, proposez-la oralement ; le responsable "
                "l'enregistre s'il l'adopte. Pour garder une trace d'un avis différent, utilisez une réserve.",
            ),
            HelpSection(
                "Réserves et désaccords",
                "`/reunion reserve texte:Risque sur les délais type:Désaccord` (participants). Sans `type`, c'est une réserve.",
            ),
            HelpSection(
                "Actions, responsables et échéances",
                "`/reunion action description:Écrire le README responsable:@Bob echeance:05/11` (participants). "
                "Échéance au format 2026-11-05, 05/11/2026 ou 05/11.",
            ),
            HelpSection(
                "Points et notes",
                "`/reunion point texte:Choix du moteur audio` ajoute un point à la fin de l'ordre du jour, avant ou "
                "pendant la réunion (participants).",
            ),
        ),
    ),
    HelpPage(
        "terminer",
        "Terminer",
        "Clôture, compte rendu, publication et récupération",
        "",
        (
            HelpSection(
                "Terminer la réunion",
                "Bouton **Terminer** ou `/reunion terminer`, puis **Confirmer** (2 minutes). Responsable ou "
                "administrateur. Pour abandonner une réunion : `/reunion annuler` (son historique est conservé).",
            ),
            HelpSection("L'étape en cours", "Elle est marquée traitée ; les étapes non abordées apparaissent « non traitée » dans le compte rendu."),
            HelpSection(
                "Le compte rendu",
                "Il est figé à la clôture (il ne change plus) puis publié dans le salon des comptes rendus "
                "(à défaut celui des décisions, sinon le salon du panneau), en 10 messages au plus.",
            ),
            HelpSection(
                "Consulter",
                "Bouton **Compte rendu** du panneau ou `/reunion compte-rendu reunion:3`. Avant la clôture, c'est un "
                "aperçu provisoire.",
            ),
            HelpSection(
                "Récupérer un compte rendu supprimé",
                "`/reunion compte-rendu reunion:3 publier:True` (responsable ou administrateur) le republie, sans "
                "doublon s'il existe encore.",
            ),
        ),
    ),
    HelpPage(
        "permissions",
        "Permissions et dépannage",
        "Qui peut faire quoi, erreurs courantes",
        "",
        (
            HelpSection(
                "Qui peut faire quoi",
                "• **Tous les membres** : voir, lister, progression, compte rendu, rejoindre, quitter.\n"
                "• **Participants** : ajouter un point, une réserve, un désaccord, une action.\n"
                "• **Responsable ou administrateur** : démarrer, changer d'étape, décision officielle, inviter, "
                "terminer, annuler, déplacer le panneau, republier le compte rendu.\n"
                "Administrateur du bot : « Gérer le serveur » ou rôle `ADMIN_ROLE_NAME`.",
            ),
            HelpSection(
                "Messages d'erreur",
                "• « Accès refusé » : action réservée au responsable ou aux administrateurs.\n"
                "• « Rejoignez d'abord la réunion » : cliquez sur Rejoindre.\n"
                "• « Impossible de … : la réunion est prévue / terminée » : l'action n'est pas possible dans cet état "
                "(une décision demande une réunion démarrée).\n"
                "• « Plusieurs réunions possibles » : précisez l'option `reunion`.\n"
                "• « Trop de demandes » : 30 actions par minute au plus, patientez quelques secondes.",
            ),
        ),
    ),
)
