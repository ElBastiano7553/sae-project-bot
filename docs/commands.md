# Commandes du bot et permissions

Toutes les réponses sont **éphémères** : seule la personne qui lance la commande
les voit (exceptions : le panneau et le compte rendu des réunions, publics). Aucune commande ne peut notifier `@everyone`, un rôle ou un membre :
les mentions sont désactivées pour tous les messages du bot (seul le message d'accueil notifie le nouveau membre lui-même).

## Organisation des commandes (1.4.0)

Depuis la version 1.4.0, les commandes sont regroupées par système : **8 commandes** au premier
niveau au lieu de 16, chacune avec ses sous-commandes. Taper `/reunion` montre tout ce qui concerne
les réunions, `/admin` tout ce qui concerne l'administration, etc.

| Groupe | Pour | Sous-commandes |
|---|---|---|
| `/aide` | tous | (commande simple) |
| `/etat` | tous | (commande simple) |
| `/roles` | tous | (commande simple) |
| `/activite` | tous | `resume`, `evenements`, `stats`, `contributions` |
| `/projet` *(1.5.0)* | tous | `liste`, `details` (les commandes d'administration arrivent avec les étapes suivantes, voir [projets.md](projets.md)) |
| `/ia` | tous | `question`, `bilan`, `relecture` |
| `/reunion` | tous | `aide`, `creer`, `voir`, `liste`, `rejoindre`, `quitter`, `inviter`, `animer`, `progression`, `ajouter point·decision·reserve·action`, `terminer`, `annuler`, `compte-rendu` |
| `/admin` | admins | `installer`, `actualiser`, `reconstruire`, `messages`, `annonce`, `config`, `diagnostic`, `historique`, `sauvegarde` |
| `/refonte` | admins | `lancer`, `retour`, `journal` |

La version 1.5.0 ajoute une 9ᵉ commande, `/projet`, demandée pour le suivi de plusieurs projets.

Choix retenus :

* noms en français, comme le reste du serveur ;
* un groupe par système, pas de commande en double : `/health` (ancien nom de `/status`) disparaît ;
* `/reunion animer etape:…` remplace `demarrer`, `suivante` et `precedente` (les boutons du panneau
  restent le moyen le plus simple) ; ce qu'on note pendant une réunion est regroupé sous `/reunion ajouter` ;
* `terminer` et `annuler` restent des sous-commandes séparées : ce sont des actions définitives, avec confirmation ;
* `/refonte` était déjà un groupe (`lancer`, `retour`, `journal`) : inchangé.

Aucune fonction n'a été retirée. Les boutons existants (panneaux de réunion, aide, confirmations) ne
dépendent pas des noms de commandes et continuent de fonctionner.

### Anciens noms

| Avant 1.4.0 | Depuis 1.4.0 |
|---|---|
| `/help` | `/aide` |
| `/status`, `/health` | `/etat` |
| `/project` | `/activite resume` |
| `/events` · `/stats` · `/leaderboard` | `/activite evenements` · `/activite stats` · `/activite contributions` |
| `/ask` · `/summary` · `/review` | `/ia question` · `/ia bilan` · `/ia relecture` |
| `/setup` · `/refresh` · `/config` · `/announce` | `/admin installer` · `/admin actualiser` · `/admin config` · `/admin annonce` |
| `/admin rebuild` · `/admin history` · `/admin backup` | `/admin reconstruire` · `/admin historique` · `/admin sauvegarde` |
| `/reunion help` | `/reunion aide` |
| `/reunion demarrer` · `suivante` · `precedente` | `/reunion animer etape:Démarrer` · `Étape suivante` · `Étape précédente` |
| `/reunion point` · `decision` · `reserve` · `action` | `/reunion ajouter point` · `decision` · `reserve` · `action` |

Discord met à jour la liste au redémarrage du bot (synchronisation automatique). Si d'anciens noms
s'affichent encore, rechargez Discord (Ctrl+R).

« Admins du bot » : membres ayant la permission Discord **Gérer le serveur**, ou
le rôle indiqué dans `ADMIN_ROLE_NAME`. Les commandes d'administration sont
protégées deux fois :

1. Discord les masque par défaut aux membres sans « Gérer le serveur »
   (un administrateur du serveur peut changer cette visibilité dans
   *Paramètres du serveur > Intégrations*) ;
2. le bot **revérifie** les droits à chaque appel, quelle que soit la visibilité.

## Consultation (tous les membres)

| Commande | Effet |
|---|---|
| `/aide [section]` | Centre d'aide en 6 pages (Accueil, Réunions, Commandes, Organisation, Administration, Dépannage) avec menu de sections, boutons Précédent / Accueil / Suivant et indicateur de page. La page « Commandes » est générée à partir des commandes enregistrées dans Discord. Les commandes et sections d'administration n'apparaissent que pour les admins du bot. Les boutons ne servent qu'à la personne qui a ouvert l'aide et disparaissent après 10 minutes. |
| `/etat` | État mesuré à l'instant : connexion Discord (latence), base SQLite (version du schéma), serveur Discord et salons, messages permanents (publiés, en erreur, liens à compléter), webhooks GitLab (écoute locale ; l'accès depuis Internet n'est **pas** vérifié), IA (fournisseur, modèle, demandes du jour ; la clé n'est pas testée pour ne pas consommer de demande), historique, alertes, dernière erreur (une ligne, sans trace d'appel ni secret), version et durée de fonctionnement. Répondre à `/etat` ne prouve pas que le bot est disponible en continu. |
| `/roles` | Choix de ses rôles : menu privé à choix multiples, déjà coché avec vos rôles ; valider ajoute et retire les rôles en une fois. Seuls les rôles proposés (domaines de travail, notifications) peuvent changer. Aussi par le bouton **Choisir mes rôles** des messages de bienvenue et du salon des rôles. Guide : [accueil-et-roles.md](accueil-et-roles.md). |
| `/activite resume` | Liens configurés (et nombre de liens à compléter), branche principale d'après le dernier webhook reçu, dernier pipeline sur cette branche, dernières merge requests, derniers pipelines et derniers pushs **reçus par le bot**. Aucun statut global n'est calculé. |

## Activité des dépôts : `/activite` (tous les membres)

Calculs détaillés et limites : [history-and-stats.md](history-and-stats.md).

| Commande | Effet |
|---|---|
| `/activite evenements [type] [periode] [recherche]` | 15 derniers événements GitLab reçus, filtrés par type (pushs, MR, pipelines, déploiements), période (24 h à tout l'historique, 7 jours par défaut) et texte (titre, branche, auteur). Le type « Erreurs du bot » est réservé aux admins du bot. |
| `/activite stats [periode]` | Commits, pushs, MR ouvertes et fusionnées, pipelines terminés, déploiements, écart avec la période précédente quand l'historique la couvre. |
| `/activite contributions [periode] [details]` | Contributions par membre : MR fusionnées, approbations, jours actifs. Les commits ne comptent pas. Pas de classement sans assez de données. |

## Assistant IA : `/ia` (tous les membres, si `AI_PROVIDER` est configuré)

Configuration, coûts et sécurité : [ai.md](ai.md).

| Commande | Effet |
|---|---|
| `/ia question question` | Réponse de l'IA à partir des documents de l'équipe (`knowledge/`, planning, liens, messages permanents, docs), avec les sources citées. |
| `/ia bilan [periode]` | Bilan de l'activité GitLab (24 h, 7 jours, 30 jours) rédigé à partir des événements reçus. |
| `/ia relecture mr` | Relecture d'une merge request lue sur GitLab en lecture seule (nécessite `GITLAB_ACCESS_TOKEN`). |

Quotas : `AI_DAILY_LIMIT` demandes par jour pour l'équipe, `AI_COOLDOWN_SECONDS` entre
deux demandes d'un membre, une demande en cours par membre.

## Réunions : `/reunion` (Samae 2.0, lot 1)

Guide complet : [meetings.md](meetings.md). L'option `reunion` est facultative : par
défaut, la réunion prévue ou en cours de ce salon (ou celle dont vous êtes participant).

| Commande | Qui | Effet |
|---|---|---|
| `/reunion aide [sujet]` | tous | Aide des réunions en 6 pages : créer, participer, animer, décisions et réserves, terminer, qui peut faire quoi. |
| `/reunion creer titre [objectifs] [etapes] [responsable] [participants] [duree] [description]` | tous | Crée la réunion et publie son panneau dans le salon courant. Étapes séparées par « ; », participants mentionnés avec `@`. |
| `/reunion voir [reunion] [publier]` | tous ; `publier` : responsable ou admin | Affiche la réunion ; `publier:True` déplace le panneau dans ce salon (nouveau panneau publié avant la suppression de l'ancien). |
| `/reunion liste [statut]` | tous | Réunions prévues ou en cours, terminées, annulées ou toutes. |
| `/reunion rejoindre` · `quitter` | tous | Rejoindre ou quitter (aussi par les boutons du panneau). |
| `/reunion inviter membre` | responsable ou admin | Ajoute un participant. |
| `/reunion animer etape [reunion]` | responsable ou admin | Démarrer, étape suivante ou étape précédente (aussi par les boutons du panneau). |
| `/reunion progression` | tous | Étapes traitées, étape en cours, temps écoulé et durée prévue. |
| `/reunion ajouter point texte` | participants | Ajoute un point à la fin de l'ordre du jour. |
| `/reunion ajouter decision texte [motif]` | responsable ou admin | Enregistre une décision (réunion en cours). |
| `/reunion ajouter reserve texte [type]` | participants | Enregistre une réserve ou un désaccord (réunion en cours). |
| `/reunion ajouter action description responsable [echeance]` | participants | Ajoute une action à faire et son responsable (réunion en cours). |
| `/reunion terminer` · `annuler` | responsable ou admin | Clôture après confirmation (2 min) ; `terminer` fige et publie le compte rendu. |
| `/reunion compte-rendu [reunion] [publier]` | tous ; `publier` : responsable ou admin | Compte rendu paginé (aperçu provisoire avant la fin) ; `publier:True` republie un compte rendu supprimé. |

## Administration : `/admin` et `/refonte` (admins du bot)

| Commande | Effet | Confirmation |
|---|---|---|
| `/refonte lancer [profil] [style] [facultatifs] [roles] [permissions] [suppressions]` | Compare le serveur à `config/server.toml` et le transforme : renomme au style choisi (emojis), déplace, corrige sujets, permissions et ordre, crée les catégories, salons et rôles colorés manquants, garde les éléments personnalisés et inconnus. Sauvegarde la base avant, journal détaillé, vérification finale. Les suppressions (doublons vides, catégories vidées, éléments déclarés obsolètes) ne sont proposées qu'avec `suppressions:True`, dans une liste séparée. Exige que l'admin ait lui-même « Gérer les salons » (et « Gérer les rôles » pour les rôles et permissions). Guide : [refonte.md](refonte.md). | Analyse → aperçu → **Appliquer les changements** → **Confirmer l'application** (10 min) ; suppressions : **Examiner** → **Supprimer ces éléments** → **Confirmer les suppressions** |
| `/refonte retour [journal]` | Rétablit ce qu'une refonte a changé, d'après son journal : noms, catégories, permissions, rôles, positions, associations. Ce qui a changé depuis est laissé tel quel ; les éléments créés sont proposés à la suppression séparément ; les messages supprimés ne reviennent jamais. | Aperçu + **Confirmer l'application** |
| `/refonte journal` | Dernières refontes, suppressions et retours (état, réussites, échecs, auteur). | — |
| `/admin installer` | Compare `config/server.toml` au serveur, puis enregistre les salons reconnus et publie ou met à jour les messages permanents. Ne crée ni ne supprime aucun salon. | Prévisualisation + bouton **Confirmer** (2 min) |
| `/admin actualiser [message]` | Met à jour les messages permanents **sur place** (même lien, réactions conservées) et recrée ceux qui ont été supprimés. `message` limite l'actualisation à un seul message. | Non (aucune perte possible) |
| `/admin config` | Réglages (sans aucun secret), salons associés, liens vers les messages gérés. | — |
| `/admin annonce` | Formulaire pour rédiger l'annonce en cours. Le message « Annonce en cours » du salon des annonces est **modifié**, jamais republié. | Prévisualisation + **Confirmer** |
| `/admin messages` | État de chaque message permanent : lien, dernière action, dernière vérification, dernière erreur, liens à compléter. | — |
| `/admin reconstruire message` | Publie un nouveau message puis supprime l'ancien (le lien et les réactions sont perdus). À réserver aux messages qui ne peuvent plus être modifiés. | Avertissement + **Confirmer** |
| `/admin diagnostic` | Permissions du bot dans chaque salon utilisé (messages permanents, notifications GitLab et GitHub), rôles proposés dans `/roles` (et pourquoi un rôle est écarté), accueil des nouveaux membres, état des intégrations. | — |
| `/admin historique` | 15 dernières actions d'administration (qui, quoi, quand, résultat). | — |
| `/admin sauvegarde` | Sauvegarde vérifiée de la base, tout de suite (nom, taille, nombre de sauvegardes conservées). Rien n'est supprimé hors des anciennes sauvegardes du bot. | — |

## Permissions Discord nécessaires au bot

Le bot n'a **pas** besoin de la permission Administrateur. Il n'utilise aucun intent privilégié, sauf
« Server Members » si l'accueil des nouveaux membres est activé (`WELCOME_ON_JOIN=true`, voir
[accueil-et-roles.md](accueil-et-roles.md)). Le choix des rôles demande **Gérer les rôles**, avec le rôle
de Samae placé au-dessus des rôles proposés.

Noms des salons : avant / après `/refonte` (par exemple `#decisions-et-comptes-rendus` devient `#✅・décisions`,
et les comptes rendus vont dans le nouveau `#📝・comptes-rendus`). Le bot les retrouve par identifiant.

`/refonte` demande en plus **Gérer les salons** et, pour les rôles et les permissions des salons, **Gérer les rôles**,
avec un rôle du bot placé au-dessus des rôles qu'il gère. Le bot doit aussi avoir lui-même chaque permission qu'il
pose sur un salon (voir le détail dans [refonte.md](refonte.md#7-permissions-nécessaires)).

| Salons | Permissions |
|---|---|
| Salons des messages permanents (`#bienvenue`, `#règlement` ou à défaut `#annonces`, `#annonces`, `#rôles` ou à défaut `#bienvenue`, `#planning`, `#décisions`, `#réunions`, `#documentation-et-preuves`, `#activité-git`) | Voir le salon, Envoyer des messages, Intégrer des liens, Voir les anciens messages, Épingler des messages (si `PIN_PERMANENT_MESSAGES=true`) |
| Salons des notifications GitLab (`#gitlab-activite`, `#builds-et-tests`) | Voir le salon, Envoyer des messages, Intégrer des liens (et Voir les anciens messages, Épingler pour le tableau de bord de `#gitlab-activite`) |
| Salon des alertes (`ALERTS_CHANNEL`, facultatif) | Voir le salon, Envoyer des messages, Intégrer des liens |
| Salons où sont créées les réunions, et `#comptes-rendus` (ou, avant la refonte, `#decisions-et-comptes-rendus`) pour les comptes rendus | Voir le salon, Envoyer des messages, Intégrer des liens, Voir les anciens messages (pour modifier le panneau) |

`/admin diagnostic` indique, salon par salon, les permissions qui manquent.
