# SAE Project Bot

Bot Discord de l'équipe SAE EPITA (serveur « SAE · GAME DEV »). Il centralise les
informations du projet, publie des messages permanents tenus à jour, relaie
l'activité GitLab et, dans les lots suivants, proposera historique, statistiques
et bilans assistés par IA.

## État actuel : version 1.2.0 (refonte du serveur, centre d'aide, identité visuelle)

| Fonction | État |
|---|---|
| Reconnaissance des catégories et salons existants (`/setup`, sans jamais en créer) | ✅ V1 |
| Refonte du serveur (`/refonte`) : analyse, aperçu paginé, confirmation, création et renommage **sans aucune suppression**, sauvegarde et journal, configuration mise à jour par identifiant | ✅ 1.2.0 |
| Centre d'aide interactif : `/help` (6 pages, menu, navigation) et `/reunion help` | ✅ 1.2.0 |
| Messages permanents idempotents : mise à jour sur place, recréation automatique s'ils sont supprimés, déplacement sans doublon, verrou contre les actualisations simultanées | ✅ V1 + lot 1 |
| Liens non configurés : version provisoire « À compléter » (obligatoire / facultatif), sans URL inventée | ✅ lot 1 |
| Annonce en cours modifiable depuis Discord (`/announce`, prévisualisation + confirmation) | ✅ lot 1 |
| Identité visuelle sobre (palette gris-bleu, très peu d'emojis, libellés de boutons explicites, horodatages Discord, limites respectées) | ✅ lot 1, revue en 1.2.0 |
| Notifications GitLab par webhooks : push (fichiers, hash, compare), merge requests (relecteurs, statut), pipelines (commit, durée, origine) ; doublons écartés même sous un autre identifiant | ✅ V2 + lot 1 |
| `/help`, `/status`, `/project`, `/admin messages`, `/admin rebuild`, `/admin diagnostic`, `/admin history` | ✅ lot 1 |
| Historique GitLab consultable (`/events`), statistiques (`/stats`), contributions (`/leaderboard`), déploiements GitLab, tableau de bord permanent, alertes d'erreurs répétées | ✅ lot 2 |
| IA (désactivée par défaut) : `/ask` (questions sur les documents de l'équipe, sources citées), `/summary` (bilan GitLab), `/review` (relecture d'une MR, API GitLab en lecture seule), quotas, secrets masqués | ✅ lot 3 |
| Sauvegardes SQLite vérifiées (automatiques, avant migration, `/admin backup`), arrêt propre, contrôle de santé, image Docker non root et guide de déploiement Linux | ✅ lot 4 (déploiement réel à faire) |
| Réunions (`/reunion`) : ordre du jour, participants, étapes, décisions, réserves, actions, panneau mis à jour sur place avec boutons persistants, compte rendu figé et paginé | ✅ Samae 2.0 lot 1 |
| Idées et tier lists · débats, sondages, auto-rôles, ressources · finitions | ⏳ Samae 2.0 lots 2 à 4 ([plan](docs/samae-2-plan.md)) |

`/help` ne liste que les commandes réellement présentes (liste générée depuis Discord, vérifiée par les tests). Déploiement sur un serveur Linux :
[docs/deployment-linux.md](docs/deployment-linux.md) ; sécurité : [docs/security.md](docs/security.md).

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
`#gitlab-et-commits` après `/refonte` : push, merge requests) et le salon des builds
(`#builds-et-tests`, `#tests-et-builds` après `/refonte` : pipelines). Pour l'activer : un secret dans
`.env`, un tunnel (Cloudflare ou ngrok, car un PC n'est pas joignable depuis
GitLab) et un webhook dans GitLab. Tout est expliqué pas à pas dans
[docs/gitlab-webhooks.md](docs/gitlab-webhooks.md), avec un script de test local :

```powershell
python -m src.gitlab.demo push
```

## Commandes

| Commande | Qui | Effet |
|---|---|---|
| `/help [section]` | tout le monde | centre d'aide en 6 pages : accueil, réunions, commandes, organisation, administration, dépannage |
| `/status` (ancien nom : `/health`) | tout le monde | état mesuré du bot, de la base, des salons, des messages, de GitLab et de l'IA, dernière erreur |
| `/project` | tout le monde | liens configurés, branche principale, derniers événements GitLab reçus |
| `/events [type] [periode] [recherche]` | tout le monde (type « erreurs » : admins) | historique des événements GitLab reçus |
| `/stats [periode]` | tout le monde | commits, MR, pipelines, déploiements, évolution |
| `/leaderboard [periode] [details]` | tout le monde | contributions (MR fusionnées, approbations, jours actifs ; pas les commits) |
| `/ask question` · `/summary [periode]` · `/review mr` | tout le monde, si l'IA est activée | réponses de l'IA, éphémères, avec leurs sources |
| `/reunion help [sujet]` | tout le monde | aide des réunions en 6 pages |
| `/reunion creer` · `voir` · `liste` · `rejoindre` · `quitter` · `progression` · `point` · `reserve` · `action` · `compte-rendu` | tout le monde (contributions : participants) | réunions, panneau public mis à jour sur place ([guide](docs/meetings.md)) |
| `/reunion demarrer` · `suivante` · `precedente` · `decision` · `inviter` · `terminer` · `annuler` | responsable de la réunion ou admins | déroulement de la réunion, décisions, clôture avec compte rendu |
| `/refonte [facultatifs] [roles]` | admins | réorganise le serveur selon `config/server.toml` : aperçu, confirmation, application sans suppression ([guide](docs/refonte-et-aide.md)) |
| `/setup` | admins | prévisualise la structure et les messages, puis applique après clic sur **Confirmer** (2 min) |
| `/refresh [message]` | admins | met à jour les messages permanents sur place, recrée ceux qui ont été supprimés |
| `/config` | admins | réglages (sans secret), salons associés, liens vers les messages gérés |
| `/announce` | admins | rédige ou modifie l'annonce en cours de `#annonces` (prévisualisation puis confirmation) |
| `/admin messages` · `rebuild` · `diagnostic` · `history` | admins | état des messages, reconstruction confirmée, permissions par salon, journal des actions |

« Admins » : membres ayant la permission « Gérer le serveur » ou le rôle défini
dans `ADMIN_ROLE_NAME` (vérifié par le bot à chaque appel). Toutes les réponses
sont visibles uniquement par la personne qui lance la commande (sauf le panneau et le
compte rendu des réunions, publics). Détails et
permissions nécessaires : [docs/commands.md](docs/commands.md).

## Modifier le contenu

| Je veux… | Fichier | Puis |
|---|---|---|
| changer le texte d'un message permanent | `content/messages/<salon>.toml` | `/refresh` |
| ajouter un lien (README, GitLab, journal IA…) ou le rendre obligatoire | `config/links.toml` | `/refresh` |
| publier ou modifier l'annonce en cours | Discord : `/announce` | (immédiat après confirmation) |
| ajouter une échéance ou une prochaine étape | `config/planning.toml` | `/refresh` |
| adapter le nom d'un salon | `config/server.toml` | `/refonte` (ou `/setup` si le salon a déjà ce nom) |
| choisir les salons, branches et événements GitLab notifiés | `config/gitlab.toml` | redémarrer le bot |
| donner des informations sur le projet à `/ask` | `knowledge/*.md` | (immédiat) |

## Sécurité

- Aucun secret dans le code : tout passe par `.env`, ignoré par Git.
- Le bot n'a pas besoin de la permission Administrateur ni d'aucun intent privilégié.
- Aucun message du bot ne peut notifier `@everyone`, un rôle ou un membre (mentions désactivées partout).
- Les secrets définis dans `.env` sont masqués (`***`) dans tous les journaux.
- IA : secrets masqués avant tout envoi, quotas, réponses éphémères, aucun texte de question ou de réponse conservé ; l'API GitLab n'est utilisée qu'en lecture seule (`read_api`).
- Les erreurs montrées sur Discord ne contiennent ni trace d'appel ni détail interne.
- Il ne supprime aucun salon, catégorie, rôle ou message d'utilisateur ; il ne supprime que
  ses propres messages permanents (déplacement de salon, reconstruction confirmée).
- Seule `/refonte`, après aperçu et confirmation d'un admin qui a lui-même « Gérer les salons »,
  crée ou renomme des catégories, salons et (sur demande) rôles. Les rôles créés n'ont aucune permission.
- Webhooks GitLab : secret vérifié à temps constant avant toute lecture, JSON et taille
  contrôlés, événements dédupliqués, textes GitLab nettoyés (aucune mention possible).
- Les commandes d'administration sont protégées deux fois : permission par défaut
  côté Discord et vérification côté bot.

## Documentation

- [Architecture, flux de données et plan des lots](docs/architecture.md)
- [Commandes et permissions](docs/commands.md)
- [Guide d'installation Windows](docs/installation-windows.md)
- [Configurer et tester les notifications GitLab (V2)](docs/gitlab-webhooks.md)
- [Historique, statistiques, classement, tableau de bord et alertes (lot 2)](docs/history-and-stats.md)
- [Assistant IA : configuration, coûts, sécurité (lot 3)](docs/ai.md)
- [Samae 2.0 : audit et plan des lots](docs/samae-2-plan.md)
- [Réunions : déroulement, permissions, compte rendu (Samae 2.0, lot 1)](docs/meetings.md)
- [Refonte du serveur, centre d'aide et identité visuelle (1.2.0)](docs/refonte-et-aide.md)

## Tests

```powershell
python -m pytest
```

Les tests n'utilisent ni token, ni réseau, ni vrai serveur Discord : Discord est
remplacé par une fausse implémentation en mémoire.
