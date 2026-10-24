# Projets suivis et multi-projets

Samae peut suivre plusieurs projets GitHub et GitLab, chacun rattaché à **un** serveur Discord.
Le travail est découpé en étapes, chacune livrée dans sa propre pull request. Cette page décrit
ce qui existe aujourd'hui (étape **B1**, version 1.5.0) et ce qui suit.

## 1. Ce que fait la version 1.5.0 (étape B1)

| Commande | Qui | Effet |
|---|---|---|
| `/projet liste` | tout le monde | projets suivis sur **ce** serveur : fournisseur, dépôt, état, référence ; au-delà de 10 projets, pages avec boutons « Précédente » / « Suivante » |
| `/projet details projet:<référence>` | tout le monde | dépôt, état et salons des notifications (autocomplétion sur la référence) |

* Un **membre** ne voit que les salons auxquels il a accès ; les autres sont comptés
  (« 1 salon(s) masqué(s) ») sans être nommés.
* Un **administrateur** (permission « Gérer le serveur » ou rôle `ADMIN_ROLE_NAME`) voit tous les
  salons, y compris ceux qui ne sont pas associés, qui a ajouté le projet et où se trouve sa configuration.
* Toutes les réponses sont éphémères. Aucune commande n'affiche de secret, de token ni de valeur du `.env`.

L'ajout et la suppression de projets depuis Discord arrivent aux étapes B4 à B6.

## 1 bis. Salons et filtres par projet (étape B2, version 1.6.0)

`/projet modifier` est réservée aux **administrateurs** : le bot le vérifie à chaque appel, et
la commande n'apparaît pas dans l'aide des membres. Elle ne s'applique qu'aux projets
enregistrés : un projet historique répond « Projet historique » et rien n'est changé.

| Option | Effet |
|---|---|
| `projet` | référence du projet (autocomplétion : projets enregistrés seulement) |
| `categorie` | Code (commits), Revues, Tickets, Intégration continue, Versions, Déploiements |
| `salon` | salon où publier cette catégorie ; obligatoire la première fois |
| `branches` | branches suivies, séparées par des virgules, jokers acceptés (`main, feature/*`), au plus 20 ; `toutes` efface le filtre. Pour Code et Intégration continue |
| `filtre` | actions ou statuts notifiés, séparés par des virgules ; `toutes` efface le filtre (voir ci-dessous) |
| `retirer` | ne plus publier cette catégorie (supprime son salon et ses filtres) ; s'utilise seul |

Une option non donnée ne change rien ; une catégorie sans filtre reçoit tout.

| Catégorie | `filtre` accepte |
|---|---|
| Revues | `ouverture`, `reouverture`, `modification`, `fusion`, `fermeture`, `approbation` |
| Tickets | `ouverture`, `fermeture`, `reouverture` |
| Intégration continue | `reussi`, `echoue`, `annule` |
| Déploiements | `reussi`, `echoue`, `annule`, `en_cours` |

Ce vocabulaire est commun à GitHub et GitLab : chaque fournisseur le traduira à la réception des
événements (étape B3). Contrôles faits avant d'enregistrer :

* le salon appartient à **ce** serveur, est textuel (ou d'annonces) et Samae peut y voir, écrire et
  intégrer des liens (sinon la permission manquante est nommée) ;
* le projet appartient à ce serveur (sinon « Projet introuvable ») ;
* les valeurs sont validées ; en cas d'erreur, rien n'est modifié.

Chaque modification est notée dans le journal d'administration (`/admin historique`) et la réponse
montre la catégorie avant et après. **Les filtres sont enregistrés mais pas encore appliqués** :
aucun événement n'arrive pour les projets enregistrés avant l'étape B3. Les webhooks historiques
du `.env` gardent leurs propres réglages (`config/gitlab.toml`), inchangés.

### Projets « historiques » (configurés dans `.env`)

La configuration existante continue de fonctionner **sans aucun changement** :

* `GITHUB_WEBHOOK_SECRET` rempli → le dépôt `GITHUB_WEBHOOK_REPOSITORY` apparaît comme
  `env-github`, avec le salon `gitlab_activite` pour le code, les revues, les tickets et la CI ;
* `GITLAB_WEBHOOK_SECRET` rempli → le projet GitLab apparaît comme `env-gitlab`, avec les salons
  de `config/gitlab.toml` (push → code, merge requests → revues, pipelines → CI, déploiements).

Ils sont **lus** depuis `.env` à chaque commande, jamais copiés en base : pour les modifier, on
modifie `.env` (ou `config/gitlab.toml`) sur le serveur qui héberge Samae, puis on redémarre.
Une erreur dans `.env` est affichée comme « désactivé : erreur dans .env » (détails dans `/etat`).
Ils n'apparaissent que sur le serveur `DISCORD_GUILD_ID`.

## 2. Modèle de données (migration v9)

| Table | Contenu |
|---|---|
| `projects` | un projet = serveur Discord (`guild_id`) + fournisseur (`github` ou `gitlab`) + identifiant chez le fournisseur (`owner/depot`, ou numéro de projet GitLab + adresse de l'instance) + nom + état (`active`, `suspended`) + auteur et date |
| `project_routes` | pour chaque projet et chaque catégorie commune (`code`, `revues`, `tickets`, `ci`, `versions`, `deploiements`) : le salon et les filtres (JSON) |

* **Aucun token ni secret** n'est stocké dans ces tables.
* Un même dépôt ne peut être suivi qu'une fois par serveur, mais plusieurs serveurs peuvent suivre le même dépôt.
* **Isolation par serveur** : chaque lecture et chaque écriture du dépôt de données
  (`src/database/projects.py`) exige l'identifiant du serveur. Un serveur ne peut ni lire, ni
  modifier, ni supprimer le projet d'un autre, même en connaissant son numéro (tests dans
  `tests/test_projects.py`).
* Supprimer un projet supprime ses salons et filtres (`ON DELETE CASCADE`).

La migration v9 **ne crée que deux tables** : rien d'existant n'est modifié. Comme pour toute
migration, Samae fait d'abord une sauvegarde vérifiée de la base
(`data/backups/…-avantv9.sqlite3`) ; si elle échoue, la migration n'a pas lieu. Retour arrière :
restaurer cette sauvegarde avec l'ancienne version (voir [deployment-vps.md](deployment-vps.md)).

### Code

| Fichier | Rôle |
|---|---|
| `src/services/projects.py` | modèle commun (fournisseurs, catégories, validation), projets historiques, visibilité ; sans Discord |
| `src/database/projects.py` | dépôt des tables `projects` et `project_routes`, toujours filtré par serveur |
| `src/discord_bot/cogs/projects.py` | groupe `/projet` |
| `src/discord_bot/embeds.py` | `projects_list_pages` (10 projets par page), `project_detail_embed` |

Les serveurs où `/projet` est ouvert sont donnés par `ProjectsCog.project_guilds()` : pour
l'instant, seulement `DISCORD_GUILD_ID`. L'ouverture à d'autres serveurs est l'étape B7.

## 3. Suite

| Étape | Contenu | Besoin côté VPS ou fournisseur |
|---|---|---|
| ✅ B1 | socle des projets, `/projet liste` et `details` | non |
| ✅ B2 | salons et filtres par projet, `/projet modifier` (admins) | non |
| B3 | réception commune des événements (serveur 127.0.0.1:8090, modules GitHub et GitLab), guide Nginx/DNS/certificat | non (tests locaux) |
| B4 | connexion GitHub par GitHub App en lecture seule (`/projet configurer`) | App de test + adresse HTTPS |
| B5 | connexion GitLab par application OAuth en lecture seule, secret de webhook affiché une seule fois dans le navigateur | application OAuth + adresse HTTPS |
| B6 | `/projet deconnecter` avec confirmation, masquage des secrets dans les journaux | non |
| B7 | autres serveurs Discord (liste autorisée) | un serveur de test |

Règles de sécurité de toutes les étapes : aucun mot de passe ni token demandé dans Discord,
permissions minimales chez GitHub et GitLab, signatures des webhooks vérifiées, doublons rejetés,
aucun secret dans les journaux, messages, erreurs ou exports, et aucun déploiement ni action
destructive sans confirmation.
