# Samae (SAE Project Bot)

Samae est le bot Discord de l'équipe SAE EPITA (serveur « SAE · GAME DEV »). Il centralise les
informations du projet, publie des messages permanents tenus à jour, relaie
l'activité GitLab et, dans les lots suivants, proposera historique, statistiques
et bilans assistés par IA.

## État actuel : version 1.10.0 (multi-projets : `/projet`, réception commune, connexion GitHub et GitLab)

| Fonction | État |
|---|---|
| Reconnaissance des catégories et salons existants (`/admin installer`, sans jamais en créer) | ✅ V1 |
| Refonte du serveur (`/refonte lancer`) : profils et styles (emojis), rôles colorés et hiérarchie, permissions, ordre, doublons et éléments personnalisés, aperçu paginé, suppressions séparées sur double confirmation, sauvegarde, journal, retour arrière (`/refonte retour`) | ✅ 1.3.0 |
| Centre d'aide interactif : `/aide` (6 pages, menu, navigation) et `/reunion aide` | ✅ 1.2.0 |
| Messages permanents idempotents : mise à jour sur place, recréation automatique s'ils sont supprimés, déplacement sans doublon, verrou contre les actualisations simultanées | ✅ V1 + lot 1 |
| Liens non configurés : version provisoire « À compléter » (obligatoire / facultatif), sans URL inventée | ✅ lot 1 |
| Annonce en cours modifiable depuis Discord (`/admin annonce`, prévisualisation + confirmation) | ✅ lot 1 |
| Identité visuelle sobre (palette gris-bleu, très peu d'emojis, libellés de boutons explicites, horodatages Discord, limites respectées) | ✅ lot 1, revue en 1.2.0 |
| Notifications GitLab par webhooks : push (fichiers, hash, compare), merge requests (relecteurs, statut), pipelines (commit, durée, origine) ; doublons écartés même sous un autre identifiant | ✅ V2 + lot 1 |
| `/aide`, `/etat`, `/activite resume`, `/admin messages`, `/admin reconstruire`, `/admin diagnostic`, `/admin historique` | ✅ lot 1 |
| Historique GitLab consultable (`/activite evenements`), statistiques (`/activite stats`), contributions (`/activite contributions`), déploiements GitLab, tableau de bord permanent, alertes d'erreurs répétées | ✅ lot 2 |
| IA (désactivée par défaut) : `/ia question` (questions sur les documents de l'équipe, sources citées), `/ia bilan` (bilan GitLab), `/ia relecture` (relecture d'une MR, API GitLab en lecture seule), quotas, secrets masqués | ✅ lot 3 |
| Sauvegardes SQLite vérifiées (automatiques, avant migration, `/admin sauvegarde`), arrêt propre, contrôle de santé, image Docker non root et guide de déploiement Linux | ✅ lot 4 (déploiement réel à faire) |
| Réunions (`/reunion`) : ordre du jour, participants, étapes, décisions, réserves, actions, panneau mis à jour sur place avec boutons persistants, compte rendu figé et paginé | ✅ Samae 2.0 lot 1 |
| Hébergement VPS en service systemd : démarrage au boot, redémarrage après crash ou blocage, instance unique, sauvegarde et test de restauration avant chaque mise à jour, retour arrière, surveillance avec alerte Discord | ✅ 1.2.1 (installation sur le VPS à faire, [guide](docs/deployment-vps.md)) |
| Réactivité : commandes resynchronisées seulement si elles changent, mesure des commandes lentes dans `/etat`, confirmations et boutons qui signalent toujours un échec | ✅ 1.2.1 |
| Commandes regroupées par système (8 commandes au premier niveau : `/aide`, `/etat`, `/roles`, `/activite`, `/ia`, `/reunion`, `/admin`, `/refonte`) | ✅ 1.4.0 |
| Choix des rôles par les membres (`/roles`, bouton « Choisir mes rôles » persistant, menu à choix multiples, rôles de staff jamais attribuables) et message d'accueil des nouveaux membres supprimé après 24 h (facultatif) | ✅ 1.4.0 ([guide](docs/accueil-et-roles.md)) |
| Règlement publié dans `#règlement` (et non plus dans `#annonces`), panneaux « Choix des rôles » et « Organiser une réunion », pied de page « Samae » | ✅ 1.4.0 |
| Multi-projets, étape B1 : projets enregistrés par serveur Discord (GitHub et GitLab), projets du `.env` affichés comme « historiques », `/projet liste` et `/projet details` (un membre ne voit que les salons auxquels il a accès) | ✅ 1.5.0 ([guide](docs/projets.md)) |
| Multi-projets, étape B2 : salon et filtres (branches, actions, statuts) par catégorie de notifications, `/projet modifier` réservé aux administrateurs | ✅ 1.6.0 ([guide](docs/projets.md)) |
| Multi-projets : `/projet ajouter` (admins) enregistre un projet à partir de l'adresse de son dépôt GitHub ou GitLab, après récapitulatif et confirmation ; pas encore de notifications (connexion en B4/B5) | ✅ 1.7.0 ([guide](docs/projets.md)) |
| Projets masqués aux membres qui ne voient aucun de leurs salons (liste, détails, autocomplétion) ; les administrateurs voient tout | ✅ 1.7.1 ([guide](docs/projets.md)) |
| Multi-projets, étape B3 : réception commune des événements GitHub et GitLab des projets enregistrés (127.0.0.1:8090, désactivée par défaut), filtres et anti-doublons par projet, guide Nginx/DNS/certificat ; aucun projet n'est encore relié (B4/B5) | ✅ 1.8.0 ([guide](docs/projets-reception.md)) |
| Multi-projets, étape B4 : `/projet configurer` (admins) relie un dépôt GitHub par la GitHub App de Samae (lien d'installation personnel et à usage unique, vérification OAuth puis révocation, aucune clé privée ni token gardé), message de test ; désactivée sans configuration | ✅ 1.9.0 ([guide](docs/github-app.md)) |
| Multi-projets, étape B5 : `/projet configurer projet:` (admins) relie un projet GitLab par une application OAuth en lecture seule (rôle Maintainer vérifié puis accès révoqué, secret du webhook affiché une seule fois et gardé seulement sous forme d'empreinte) ; désactivée sans configuration | ✅ 1.10.0 ([guide](docs/gitlab-app.md)) |
| Multi-projets, étapes B6 et B7 : déconnexion, autres serveurs | ⏳ ([plan](docs/projets.md#suite)) |
| Idées et tier lists · débats, sondages, ressources · finitions | ⏳ Samae 2.0 lots 2 à 4 ([plan](docs/samae-2-plan.md)) |

`/aide` ne liste que les commandes réellement présentes (liste générée depuis Discord, vérifiée par les tests). Hébergement de l'équipe (VPS, systemd) :
[docs/deployment-vps.md](docs/deployment-vps.md) ; avec Docker : [docs/deployment-linux.md](docs/deployment-linux.md) ;
sécurité : [docs/security.md](docs/security.md) ; plan de test manuel : [docs/test-manuel.md](docs/test-manuel.md).

## Démarrage rapide (Windows, PowerShell)

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
python -m pytest
Copy-Item .env.example .env   # puis remplissez DISCORD_BOT_TOKEN et DISCORD_GUILD_ID
python -m src.main
```

Le guide détaillé, étape par étape, est dans
[docs/installation-windows.md](docs/installation-windows.md).

## Notifications GitLab (V2)

Le bot reçoit les webhooks GitLab et publie dans le salon GitLab (`#gitlab-activite`,
`#🦊・activité-git` après `/refonte` : push, merge requests) et le salon des builds
(`#builds-et-tests`, `#🧪・tests-et-builds` après `/refonte` : pipelines). Pour l'activer : un secret dans
`.env`, un tunnel (Cloudflare ou ngrok, car un PC n'est pas joignable depuis
GitLab) et un webhook dans GitLab. Tout est expliqué pas à pas dans
[docs/gitlab-webhooks.md](docs/gitlab-webhooks.md), avec un script de test local :

```powershell
python -m src.gitlab.demo push
```

## Notifications GitHub du jeu

Indépendamment de GitLab, le bot peut publier dans le même salon (clé `gitlab_activite`)
l'activité du dépôt GitHub privé `ElBastiano7553/game-project` : push, pull requests,
issues et workflows GitHub Actions en échec. Désactivé tant que `GITHUB_WEBHOOK_SECRET`
est vide ; serveur séparé sur le port 8081, signatures HMAC-SHA256 vérifiées. Guide :
[docs/github-webhooks.md](docs/github-webhooks.md), déploiement :
[docs/github-deploiement.md](docs/github-deploiement.md). Test local :

```powershell
python -m src.github.demo push
```

## Commandes

Les commandes sont regroupées par système : tapez `/` puis le nom du groupe, Discord propose ses sous-commandes.

| Commande | Qui | Effet |
|---|---|---|
| `/aide [section]` | tout le monde | centre d'aide en 6 pages : accueil, réunions, commandes, organisation, administration, dépannage |
| `/etat` | tout le monde | état mesuré du bot, de la base, des salons, des messages, de GitLab, GitHub et de l'IA, dernière erreur |
| `/roles` | tout le monde | choisir ses domaines de travail et ses notifications (menu privé, plusieurs choix, modifiable) |
| `/activite resume` · `evenements` · `stats` · `contributions` | tout le monde (type « erreurs » : admins) | liens et activité récente, historique GitLab, statistiques, contributions |
| `/projet liste` · `details` | tout le monde (détails complets : admins) | projets GitHub et GitLab suivis sur ce serveur, état et salons des notifications |
| `/projet ajouter` · `modifier` | admins | enregistrer un projet (adresse du dépôt, confirmation), puis choisir le salon et les filtres de chaque catégorie |
| `/ia question` · `bilan` · `relecture` | tout le monde, si l'IA est activée | réponses de l'IA, éphémères, avec leurs sources |
| `/reunion aide` · `creer` · `voir` · `liste` · `rejoindre` · `quitter` · `progression` · `compte-rendu` | tout le monde | réunions, panneau public mis à jour sur place ([guide](docs/meetings.md)) |
| `/reunion ajouter point` · `reserve` · `action` | participants | ce qu'on note pendant la réunion |
| `/reunion animer` · `ajouter decision` · `inviter` · `terminer` · `annuler` | responsable de la réunion ou admins | démarrer, changer d'étape, décisions officielles, clôture avec compte rendu |
| `/admin installer` · `actualiser` · `reconstruire` · `messages` · `annonce` | admins | associer les salons, publier et tenir à jour les messages permanents, annonce en cours |
| `/admin config` · `diagnostic` · `historique` · `sauvegarde` | admins | réglages sans secret, permissions par salon et rôles proposés, journal, sauvegarde |
| `/refonte lancer` · `retour` · `journal` | admins | réorganise le serveur selon `config/server.toml` : aperçu, confirmation, suppressions séparées, retour arrière ([guide](docs/refonte.md)) |

Correspondance avec les anciens noms (avant 1.4.0) : [docs/commands.md](docs/commands.md#anciens-noms).

« Admins » : membres ayant la permission « Gérer le serveur » ou le rôle défini
dans `ADMIN_ROLE_NAME` (vérifié par le bot à chaque appel). Toutes les réponses
sont visibles uniquement par la personne qui lance la commande (sauf le panneau et le
compte rendu des réunions, publics). Détails et
permissions nécessaires : [docs/commands.md](docs/commands.md).

## Modifier le contenu

| Je veux… | Fichier | Puis |
|---|---|---|
| changer le texte d'un message permanent | `content/messages/<nom>.toml` | `/admin actualiser` |
| changer le message d'accueil des nouveaux membres | `content/accueil.toml` | (immédiat, à la prochaine arrivée) |
| proposer un rôle de plus dans `/roles` | `config/server.toml` : `self_assignable = true` | `/admin actualiser` (panneau des rôles) |
| ajouter un lien (README, GitLab, journal IA…) ou le rendre obligatoire | `config/links.toml` | `/admin actualiser` |
| publier ou modifier l'annonce en cours | Discord : `/admin annonce` | (immédiat après confirmation) |
| ajouter une échéance ou une prochaine étape | `config/planning.toml` | `/admin actualiser` |
| adapter le nom d'un salon, un emoji, une couleur de rôle, un style | `config/server.toml` | `/refonte lancer` (ou `/admin installer` si le salon a déjà ce nom) |
| choisir les salons, branches et événements GitLab notifiés | `config/gitlab.toml` | redémarrer le bot |
| donner des informations sur le projet à `/ia question` | `knowledge/*.md` | (immédiat) |

## Sécurité

- Aucun secret dans le code : tout passe par `.env`, ignoré par Git.
- Le bot n'a pas besoin de la permission Administrateur. Le seul intent privilégié possible est
  « Server Members », demandé uniquement si l'accueil des nouveaux membres est activé (`WELCOME_ON_JOIN=true`).
- Choix des rôles : seuls les rôles « fonction » ou « notification » marqués `self_assignable`, sans
  aucune permission sensible et placés sous le rôle de Samae, peuvent être pris ou retirés par un membre.
- Aucun message du bot ne peut notifier `@everyone` ou un rôle ; seul le message d'accueil notifie le nouveau membre lui-même.
- Les secrets définis dans `.env` sont masqués (`***`) dans tous les journaux.
- IA : secrets masqués avant tout envoi, quotas, réponses éphémères, aucun texte de question ou de réponse conservé ; l'API GitLab n'est utilisée qu'en lecture seule (`read_api`).
- Les erreurs montrées sur Discord ne contiennent ni trace d'appel ni détail interne.
- Il ne supprime aucun message d'utilisateur ; hors `/refonte`, il ne supprime que ses propres
  messages permanents (déplacement de salon, reconstruction confirmée) et ses messages d'accueil enregistrés.
- Seule `/refonte`, après aperçu et confirmation d'un admin qui a lui-même « Gérer les salons »,
  crée, renomme, déplace ou modifie catégories, salons et rôles. Elle ne supprime un élément
  qu'avec `suppressions:True`, après une seconde confirmation distincte et une revérification ;
  elle ne donne jamais la permission Administrateur, et les permissions sensibles sont réservées au staff.
- Webhooks GitLab : secret vérifié à temps constant avant toute lecture, JSON et taille
  contrôlés, événements dédupliqués, textes GitLab nettoyés (aucune mention possible).
- Webhooks GitHub : signature HMAC-SHA256 vérifiée à temps constant, un seul dépôt accepté,
  livraisons dédupliquées (`X-GitHub-Delivery`), mêmes protections contre les mentions.
- Les commandes d'administration sont protégées deux fois : permission par défaut
  côté Discord et vérification côté bot.

## Documentation

- [Architecture, flux de données et plan des lots](docs/architecture.md)
- [Commandes et permissions](docs/commands.md)
- [Accueil des nouveaux membres et choix des rôles](docs/accueil-et-roles.md)
- [Projets suivis et multi-projets](docs/projets.md)
- [Guide d'installation Windows](docs/installation-windows.md)
- [Configurer et tester les notifications GitLab (V2)](docs/gitlab-webhooks.md)
- [Notifications GitHub du jeu](docs/github-webhooks.md) et [leur déploiement](docs/github-deploiement.md)
- [Historique, statistiques, classement, tableau de bord et alertes (lot 2)](docs/history-and-stats.md)
- [Assistant IA : configuration, coûts, sécurité (lot 3)](docs/ai.md)
- [Samae 2.0 : audit et plan des lots](docs/samae-2-plan.md)
- [Réunions : déroulement, permissions, compte rendu (Samae 2.0, lot 1)](docs/meetings.md)
- [Refonte du serveur : audit, configuration, permissions, retour arrière (1.3.0)](docs/refonte.md)
- [Centre d'aide et identité visuelle (1.2.0)](docs/refonte-et-aide.md)
- [Héberger Samae sur le VPS (systemd) : installation, mises à jour, retour arrière, sauvegardes, surveillance](docs/deployment-vps.md)
- [Plan de test manuel sur un serveur Discord de test (dont `/refonte`)](docs/test-manuel.md)

## Tests

```powershell
python -m pytest
```

Les tests n'utilisent ni token, ni réseau, ni vrai serveur Discord : Discord est
remplacé par une fausse implémentation en mémoire.
