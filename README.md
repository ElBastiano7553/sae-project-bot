# SAE Project Bot

Bot Discord de l'équipe SAE EPITA (serveur « SAE · GAME DEV »). Il centralise les
informations du projet, publie des messages permanents tenus à jour, relaie
l'activité GitLab et, dans les lots suivants, proposera historique, statistiques
et bilans assistés par IA.

## État actuel : V2 + lots 1 et 2 (version 0.4.0)

| Fonction | État |
|---|---|
| Reconnaissance des catégories et salons existants (sans jamais en créer) | ✅ V1 |
| Messages permanents idempotents : mise à jour sur place, recréation automatique s'ils sont supprimés, déplacement sans doublon, verrou contre les actualisations simultanées | ✅ V1 + lot 1 |
| Liens non configurés : version provisoire « À compléter » (obligatoire / facultatif), sans URL inventée | ✅ lot 1 |
| Annonce en cours modifiable depuis Discord (`/announce`, prévisualisation + confirmation) | ✅ lot 1 |
| Identité visuelle commune des embeds (vert / rouge / orange / bleu, horodatages Discord, limites respectées) | ✅ lot 1 |
| Notifications GitLab par webhooks : push (fichiers, hash, compare), merge requests (relecteurs, statut), pipelines (commit, durée, origine) ; doublons écartés même sous un autre identifiant | ✅ V2 + lot 1 |
| `/help`, `/status`, `/project`, `/admin messages`, `/admin rebuild`, `/admin diagnostic`, `/admin history` | ✅ lot 1 |
| Historique GitLab consultable (`/events`), statistiques (`/stats`), contributions (`/leaderboard`), déploiements GitLab, tableau de bord permanent, alertes d'erreurs répétées | ✅ lot 2 |
| IA : `/ask`, `/summary`, `/review` | ⏳ lot 3 |
| Sauvegardes, Docker, déploiement chez La Banquise | ⏳ lot 4 |

Rien de ce qui est marqué ⏳ n'est disponible : `/help` ne liste que les commandes réellement présentes.

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

Le bot reçoit les webhooks GitLab et publie dans `#gitlab-activite` (push,
merge requests) et `#builds-et-tests` (pipelines). Pour l'activer : un secret dans
`.env`, un tunnel (Cloudflare ou ngrok, car un PC n'est pas joignable depuis
GitLab) et un webhook dans GitLab. Tout est expliqué pas à pas dans
[docs/gitlab-webhooks.md](docs/gitlab-webhooks.md), avec un script de test local :

```powershell
python -m src.gitlab.demo push
```

## Commandes

| Commande | Qui | Effet |
|---|---|---|
| `/help` | tout le monde | aide par catégorie, avec exemples |
| `/status` (ancien nom : `/health`) | tout le monde | état mesuré du bot, de la base, des salons, des messages, de GitLab et de l'IA, dernière erreur |
| `/project` | tout le monde | liens configurés, branche principale, derniers événements GitLab reçus |
| `/events [type] [periode] [recherche]` | tout le monde (type « erreurs » : admins) | historique des événements GitLab reçus |
| `/stats [periode]` | tout le monde | commits, MR, pipelines, déploiements, évolution |
| `/leaderboard [periode] [details]` | tout le monde | contributions (MR fusionnées, approbations, jours actifs ; pas les commits) |
| `/setup` | admins | prévisualise la structure et les messages, puis applique après clic sur **Confirmer** (2 min) |
| `/refresh [message]` | admins | met à jour les messages permanents sur place, recrée ceux qui ont été supprimés |
| `/config` | admins | réglages (sans secret), salons associés, liens vers les messages gérés |
| `/announce` | admins | rédige ou modifie l'annonce en cours de `#annonces` (prévisualisation puis confirmation) |
| `/admin messages` · `rebuild` · `diagnostic` · `history` | admins | état des messages, reconstruction confirmée, permissions par salon, journal des actions |

« Admins » : membres ayant la permission « Gérer le serveur » ou le rôle défini
dans `ADMIN_ROLE_NAME` (vérifié par le bot à chaque appel). Toutes les réponses
sont visibles uniquement par la personne qui lance la commande. Détails et
permissions nécessaires : [docs/commands.md](docs/commands.md).

## Modifier le contenu

| Je veux… | Fichier | Puis |
|---|---|---|
| changer le texte d'un message permanent | `content/messages/<salon>.toml` | `/refresh` |
| ajouter un lien (README, GitLab, journal IA…) ou le rendre obligatoire | `config/links.toml` | `/refresh` |
| publier ou modifier l'annonce en cours | Discord : `/announce` | (immédiat après confirmation) |
| ajouter une échéance ou une prochaine étape | `config/planning.toml` | `/refresh` |
| adapter le nom d'un salon | `config/server.toml` | `/setup` |
| choisir les salons, branches et événements GitLab notifiés | `config/gitlab.toml` | redémarrer le bot |

## Sécurité

- Aucun secret dans le code : tout passe par `.env`, ignoré par Git.
- Le bot n'a pas besoin de la permission Administrateur ni d'aucun intent privilégié.
- Aucun message du bot ne peut notifier `@everyone`, un rôle ou un membre (mentions désactivées partout).
- Les secrets définis dans `.env` sont masqués (`***`) dans tous les journaux.
- Les erreurs montrées sur Discord ne contiennent ni trace d'appel ni détail interne.
- Il ne crée, ne renomme et ne supprime aucun salon, rôle ou message d'utilisateur ; il ne
  supprime que ses propres messages permanents (déplacement de salon, reconstruction confirmée).
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

## Tests

```powershell
python -m pytest
```

Les tests n'utilisent ni token, ni réseau, ni vrai serveur Discord : Discord est
remplacé par une fausse implémentation en mémoire.
