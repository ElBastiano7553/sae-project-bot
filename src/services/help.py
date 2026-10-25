"""Registre de l'aide (/aide, /reunion aide), sans dépendance à discord.py.

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
    "accueil": "Accueil et rôles",
    "activite": "Activité des dépôts",
    "projets": "Projets suivis",
    "ia": "Assistant IA",
    "reunions": "Réunions",
    "administration": "Administrer",
}

HELP_ENTRIES: tuple[HelpEntry, ...] = (
    HelpEntry("aide", "consultation", "Centre d'aide paginé : présentation, réunions, commandes, organisation, dépannage.", "/aide section:Dépannage"),
    HelpEntry("etat", "consultation", "État du bot : Discord, base de données, webhooks GitLab et GitHub, IA, dernière erreur.", "/etat"),
    HelpEntry(
        "roles",
        "accueil",
        "Choisir ses domaines de travail (Développement, Art & Design…) et ses notifications ; modifiable à tout moment.",
        "/roles",
    ),
    HelpEntry(
        "activite resume",
        "activite",
        "Résumé du projet : liens configurés, branche principale, derniers événements GitLab reçus.",
        "/activite resume",
    ),
    HelpEntry(
        "activite evenements",
        "activite",
        "Historique des événements GitLab reçus, filtrable par type, période et texte (type « erreurs » : admins).",
        "/activite evenements type:Pipelines periode:7 derniers jours recherche:main",
    ),
    HelpEntry(
        "activite stats",
        "activite",
        "Commits, merge requests, pipelines et déploiements sur une période, avec l'évolution.",
        "/activite stats periode:30 derniers jours",
    ),
    HelpEntry(
        "activite contributions",
        "activite",
        "Contributions : MR fusionnées, approbations, jours actifs (les commits ne comptent pas). Pas de classement sans assez de données.",
        "/activite contributions periode:30 derniers jours details:True",
    ),
    HelpEntry(
        "projet liste",
        "projets",
        "Projets GitHub et GitLab suivis sur ce serveur, avec leur état (y compris ceux configurés dans .env).",
        "/projet liste",
    ),
    HelpEntry(
        "projet details",
        "projets",
        "Détails d'un projet : dépôt suivi, état, salons des notifications (seulement ceux que vous pouvez voir).",
        "/projet details projet:env-github",
    ),
    HelpEntry(
        "projet ajouter",
        "projets",
        "Enregistrer un projet GitHub ou GitLab à partir de l'adresse de son dépôt (récapitulatif, puis confirmation).",
        "/projet ajouter url:https://github.com/equipe/jeu",
        admin=True,
    ),
    HelpEntry(
        "projet modifier",
        "projets",
        "Salon et filtres (branches, actions, statuts) d'une catégorie de notifications d'un projet enregistré.",
        "/projet modifier projet:1 categorie:Code (commits) branches:main, develop",
        admin=True,
    ),
    HelpEntry(
        "projet configurer",
        "projets",
        "Connecter un dépôt GitHub (GitHub App : installation, vérification, choix du dépôt) ou, avec projet:, un projet GitLab "
        "(autorisation GitLab, rôle Maintainer, secret du webhook affiché une fois). Aucun token dans Discord.",
        "/projet configurer",
        admin=True,
    ),
    HelpEntry(
        "projet diagnostic",
        "projets",
        "Pourquoi un projet ne reçoit pas ses notifications : réception, connexion, salons, livraisons des 7 derniers jours, et la piste à suivre.",
        "/projet diagnostic projet:1",
        admin=True,
    ),
    HelpEntry("ia question", "ia", "Question sur le projet : l'IA répond à partir des documents de l'équipe et cite ses sources.", "/ia question question:Quand est la prochaine soutenance ?"),
    HelpEntry("ia bilan", "ia", "Bilan de l'activité GitLab de la période, rédigé par l'IA à partir des événements reçus.", "/ia bilan periode:7 derniers jours"),
    HelpEntry("ia relecture", "ia", "Relecture d'une merge request par l'IA : bugs probables, tests, sécurité (une aide, pas une validation).", "/ia relecture mr:12"),
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
    HelpEntry(
        "reunion animer",
        "reunions",
        "Démarre la réunion, passe à l'étape suivante ou revient à la précédente (responsable ou admin ; aussi par les boutons du panneau).",
        "/reunion animer etape:Étape suivante",
    ),
    HelpEntry("reunion progression", "reunions", "Étapes traitées, étape en cours, temps écoulé par rapport à la durée prévue.", "/reunion progression"),
    HelpEntry("reunion ajouter point", "reunions", "Ajoute un point à la fin de l'ordre du jour (participants).", "/reunion ajouter point texte:Choix du moteur audio"),
    HelpEntry(
        "reunion ajouter decision",
        "reunions",
        "Enregistre une décision, avec un motif facultatif (responsable ou admin, réunion en cours).",
        "/reunion ajouter decision texte:On garde Pygame motif:déjà maîtrisé",
    ),
    HelpEntry(
        "reunion ajouter reserve",
        "reunions",
        "Enregistre une réserve ou un désaccord, conservé dans le compte rendu (participants).",
        "/reunion ajouter reserve texte:Risque sur les délais type:Désaccord",
    ),
    HelpEntry(
        "reunion ajouter action",
        "reunions",
        "Ajoute une action à effectuer avec son responsable et une échéance facultative.",
        "/reunion ajouter action description:Écrire le README responsable:@Bob echeance:05/11",
    ),
    HelpEntry("reunion terminer", "reunions", "Termine la réunion après confirmation, fige et publie le compte rendu (responsable ou admin).", "/reunion terminer"),
    HelpEntry("reunion annuler", "reunions", "Annule une réunion prévue ou en cours, après confirmation ; l'historique est conservé.", "/reunion annuler reunion:4"),
    HelpEntry("reunion compte-rendu", "reunions", "Compte rendu paginé (aperçu provisoire si la réunion n'est pas terminée).", "/reunion compte-rendu reunion:3"),
    HelpEntry("reunion aide", "reunions", "Guide pas à pas des réunions : créer, participer, animer, décisions, compte rendu, dépannage.", "/reunion aide sujet:Terminer"),
    HelpEntry("setup etat", "administration", "Ce qui fonctionne sur CE serveur, ce qui manque et comment le régler (sans aucun secret).", "/setup etat", True),
    HelpEntry(
        "setup serveur-principal",
        "administration",
        "Fait de ce serveur le serveur principal (messages permanents, /refonte, /ia, /activite) ; propriétaire du bot seulement.",
        "/setup serveur-principal",
        True,
    ),
    HelpEntry(
        "admin installer",
        "administration",
        "Prévisualise la structure du serveur et les messages permanents, puis associe les salons et publie après confirmation.",
        "/admin installer",
        True,
    ),
    HelpEntry("admin actualiser", "administration", "Met à jour les messages permanents sur place (tous, ou un seul).", "/admin actualiser message:liens", True),
    HelpEntry(
        "admin reconstruire",
        "administration",
        "Reconstruit un message permanent (nouveau message puis suppression de l'ancien), après confirmation.",
        "/admin reconstruire message:bienvenue",
        True,
    ),
    HelpEntry("admin messages", "administration", "État de chaque message permanent : lien, dernière action, erreur, liens à compléter.", "/admin messages", True),
    HelpEntry("admin annonce", "administration", "Rédige ou modifie l'annonce en cours du salon des annonces (prévisualisation puis confirmation).", "/admin annonce", True),
    HelpEntry("admin config", "administration", "Réglages du bot (sans aucun secret), salons associés et rôles proposés au choix.", "/admin config", True),
    HelpEntry("admin diagnostic", "administration", "Vérifie les permissions du bot dans chaque salon utilisé et pour les rôles à choisir.", "/admin diagnostic", True),
    HelpEntry("admin historique", "administration", "Journal des dernières actions d'administration.", "/admin historique", True),
    HelpEntry("admin sauvegarde", "administration", "Sauvegarde immédiate et vérifiée de la base de données (rotation automatique).", "/admin sauvegarde", True),
    HelpEntry(
        "refonte lancer",
        "administration",
        "Réorganise le serveur selon la structure cible : rôles colorés, catégories, salons, permissions, ordre ; "
        "aperçu puis confirmation. Suppressions seulement sur demande, confirmées à part.",
        "/refonte lancer profil:projet style:projet",
        True,
    ),
    HelpEntry(
        "refonte retour",
        "administration",
        "Rétablit ce qu'une refonte a changé, d'après son journal (aperçu puis confirmation).",
        "/refonte retour",
        True,
    ),
    HelpEntry("refonte journal", "administration", "Dernières refontes, suppressions et retours arrière.", "/refonte journal", True),
)

# Qui peut utiliser chaque commande de réunion (règles appliquées par services.meetings).
MEETING_CONTROL = {"inviter", "animer", "decision", "terminer", "annuler"}
MEETING_CONTRIBUTION = {"point", "reserve", "action"}


def permission_label(entry: HelpEntry) -> str:
    if entry.admin:
        return "administrateurs du bot"
    parts = entry.command.split()
    if parts[0] == "reunion" and len(parts) > 1:
        if parts[-1] in MEETING_CONTROL:
            return "responsable de la réunion ou administrateur"
        if parts[-1] in MEETING_CONTRIBUTION:
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
    for size in (3, 2, 1):
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


# Une commande citée dans un texte d'aide : « /etat », « /reunion creer », « /reunion ajouter point »
# (pas « config/server.toml »).
_MENTIONED = re.compile(r"(?:^|(?<=[\s`(«*]))/([a-z][a-z-]*)(?: ([a-z][a-z-]*))?(?: ([a-z][a-z-]*))?")


def mentioned_commands(text: str, group_names: Iterable[str]) -> set[str]:
    """Commandes citées dans un texte : « reunion creer » pour un groupe, « reunion ajouter point » pour un
    sous-groupe, « etat » sinon. ``group_names`` contient les groupes (« reunion ») et les sous-groupes
    (« reunion ajouter »)."""
    groups = set(group_names)
    found = set()
    for first, second, third in _MENTIONED.findall(text):
        name = first
        if first in groups and second:
            name = f"{first} {second}"
            if name in groups and third:
                name = f"{name} {third}"
        found.add(name)
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
                "• **Activité des dépôts** : notifications GitLab (pushs, merge requests, pipelines) et GitHub (pull "
                "requests du jeu) si les webhooks sont configurés ; `/activite` : résumé, événements, statistiques, "
                "contributions.\n"
                "• **Assistant IA**, s'il est activé : `/ia question`, `/ia bilan`, `/ia relecture`.\n"
                "• **Rôles** : chacun choisit ses domaines de travail et ses notifications avec `/roles`.\n"
                "• **Messages permanents** : bienvenue, règlement, annonces, rôles, planning, liens, tableau de bord.\n"
                "• **État** : `/etat` pour le bot, `/activite resume` pour les liens et l'activité récente.",
            ),
            HelpSection(
                "Pour commencer",
                "• `/roles` : choisir vos domaines de travail (Développement, Art & Design…).\n"
                "• `/reunion aide` : le guide des réunions, pas à pas.\n"
                "• `/activite resume` : liens du projet (dépôt, sujet, documents).\n"
                "• `/etat` : vérifier que Samae et ses intégrations fonctionnent.",
            ),
            HelpSection(
                "Naviguer",
                "Boutons **Précédent**, **Suivant** et **Accueil**, ou le menu pour aller directement à une section. "
                "`/aide section:Commandes` ouvre directement une section.",
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
                "4. Pendant la réunion : `/reunion ajouter decision` (responsable), `/reunion ajouter reserve`, `/reunion ajouter action`, "
                "`/reunion ajouter point` (participants).\n"
                "5. **Terminer** : le compte rendu est figé puis publié dans le salon des comptes rendus.",
            ),
            HelpSection(
                "Guide complet",
                "`/reunion aide` détaille chaque étape avec des exemples vérifiés, les permissions et les solutions "
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
                "administrateur (`/reunion ajouter decision`), conservées dans le compte rendu.\n"
                "• **Actions** : décidées en réunion, avec un responsable et une échéance (`/reunion ajouter action`).\n"
                "• **Réunions** : voir la page Réunions et `/reunion aide`.\n"
                "• **Suivi du code** : `/activite evenements`, `/activite stats`, `/activite contributions` et le tableau de bord GitLab.\n"
                "• **Ressources** : les liens du projet sont dans le message « Liens utiles » et `/activite resume`.\n"
                "• **Rôles de domaine** (Développement, Game Design, Art & Design, Testeur) et rôles de notification : "
                "chacun les choisit avec `/roles` ou le bouton « Choisir mes rôles », plusieurs à la fois, et peut "
                "changer quand il veut. Ils indiquent qui fait quoi et ne donnent aucun droit.\n"
                "• **Rôles de responsabilité** (Responsable projet, Modérateur…) : attribués par un administrateur, "
                "jamais par le choix des rôles.",
            ),
            HelpSection(
                "Pas encore disponible",
                "• **Idées et tier lists** (propositions, votes secrets, classements) : prévues, pas encore "
                "développées. En attendant, utilisez le salon des idées.\n"
                "• **Votes de départage, sondages, catalogue de ressources** : prévus.\n"
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
                "• `/admin installer` : retrouve les salons existants et publie les messages permanents (ne crée rien).\n"
                "• Rôles proposés aux membres : `self_assignable = true` dans `config/server.toml` ; vérification "
                "avec `/admin diagnostic`. Message d'arrivée : `WELCOME_ON_JOIN` (voir la documentation).\n"
                "• `/admin actualiser`, `/admin annonce` : messages permanents et annonce en cours.\n"
                "• `/admin config`, `/admin diagnostic`, `/admin messages`, `/admin historique` : état, permissions, journal.\n"
                "• `/admin sauvegarde` : sauvegarde immédiate de la base. `/admin reconstruire` : reconstruit un message.\n"
                "• `/refonte lancer` : réorganise le serveur selon la structure cible ; `/refonte retour` l'annule ; "
                "`/refonte journal` liste les refontes.",
                audience="admin",
            ),
            HelpSection(
                "Refonte du serveur",
                "1. `/refonte lancer` analyse le serveur sans rien modifier ; choisissez le profil et le style.\n"
                "2. **Prévisualiser** : structure cible, rôles et couleurs, créations, renommages, déplacements, "
                "permissions, ordre, éléments personnalisés ou inconnus (conservés), doublons, risques.\n"
                "3. **Appliquer les changements** puis **Confirmer** : Samae revérifie vos droits, les siens et "
                "l'état du serveur, sauvegarde sa base, relève le serveur, applique, puis vérifie.\n"
                "4. Suppressions : seulement avec `suppressions:True`, après **Examiner les suppressions** et une "
                "confirmation distincte.\n"
                "Relancer la commande sur un serveur conforme ne change rien.",
                audience="admin",
            ),
            HelpSection(
                "Permissions nécessaires",
                "• Vous : administrateur du bot, plus « Gérer les salons » pour `/refonte lancer` ; « Gérer les "
                "rôles » pour les rôles et les permissions (sinon, ils sont listés comme impossibles).\n"
                "• Le bot : « Gérer les salons » et « Gérer les rôles », son rôle placé au-dessus des rôles qu'il gère ; "
                "« Voir les salons », « Envoyer des messages », « Intégrer des liens », « Voir les anciens messages » "
                "et « Épingler » dans les salons qu'il utilise (`/admin diagnostic`).",
                audience="admin",
            ),
            HelpSection(
                "Configuration et précautions",
                "La structure cible est dans `config/server.toml` : profils, styles, catégories, salons (emoji, type, "
                "sujet, accès), rôles (couleur, permissions), niveaux d'accès. Samae retient les identifiants Discord : "
                "un salon renommé à la main reste reconnu. Chaque refonte laisse une sauvegarde (`data/backups`) et un "
                "journal (`data/refonte`), base de `/refonte retour`.",
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
                "Essayez `/etat`. Sans réponse du tout, Samae est arrêté ou déconnecté : prévenez la personne qui "
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
                "`/refonte lancer` : elle ne refait que ce qui manque encore. `/refonte retour` rétablit l'état d'avant.",
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
            HelpSection(
                "Démarrer",
                "Bouton **Démarrer** ou `/reunion animer etape:Démarrer` : la réunion passe « en cours » et la première étape commence.",
            ),
            HelpSection(
                "Changer d'étape",
                "Boutons **Étape suivante** et **Étape précédente**, ou `/reunion animer` avec l'option `etape`. "
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
                "`/reunion ajouter decision texte:On garde Pygame motif:déjà maîtrisé`\n"
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
                "`/reunion ajouter reserve texte:Risque sur les délais type:Désaccord` (participants). Sans `type`, c'est une réserve.",
            ),
            HelpSection(
                "Actions, responsables et échéances",
                "`/reunion ajouter action description:Écrire le README responsable:@Bob echeance:05/11` (participants). "
                "Échéance au format 2026-11-05, 05/11/2026 ou 05/11.",
            ),
            HelpSection(
                "Points et notes",
                "`/reunion ajouter point texte:Choix du moteur audio` ajoute un point à la fin de l'ordre du jour, avant ou "
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
