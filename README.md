# SAE Project Bot

Bot Discord de l'équipe SAE EPITA (serveur « SAE · GAME DEV »). Il centralise les
informations du projet, publie des messages permanents et, dans les versions
suivantes, relaiera l'activité GitLab et proposera des bilans assistés par IA.

## État actuel : V1

| Fonction | État |
|---|---|
| Reconnaissance des catégories et salons existants (sans jamais en créer) | ✅ V1 |
| Messages permanents idempotents (#bienvenue, #annonces, #planning-et-deadlines, #decisions-et-comptes-rendus, #documentation-et-preuves) | ✅ V1 |
| `/health`, `/config`, `/setup` (prévisualisation + bouton de confirmation), `/refresh` | ✅ V1 |
| Notifications GitLab par webhooks | ⏳ V2 |
| Commandes de consultation et bilans GitLab | ⏳ V3 |
| IA générative et journal IA | ⏳ V4 |

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

## Commandes

| Commande | Qui | Effet |
|---|---|---|
| `/health` | tout le monde | état du bot, de la base, des salons et des intégrations |
| `/config` | admins | réglages (sans secret), salons associés, liens vers les messages gérés |
| `/setup` | admins | prévisualise la structure et les messages, puis applique après clic sur **Confirmer** (expire après 2 min) |
| `/refresh` | admins | met à jour les messages permanents et recrée ceux qui ont été supprimés |

« Admins » : membres ayant la permission « Gérer le serveur » ou le rôle défini
dans `ADMIN_ROLE_NAME`. Toutes les réponses sont visibles uniquement par la
personne qui lance la commande.

## Modifier le contenu

| Je veux… | Fichier | Puis |
|---|---|---|
| changer le texte d'un message permanent | `content/messages/<salon>.toml` | `/refresh` |
| ajouter un lien (README, GitLab, journal IA…) | `config/links.toml` | `/refresh` |
| ajouter une échéance ou une prochaine étape | `config/planning.toml` | `/refresh` |
| adapter le nom d'un salon | `config/server.toml` | `/setup` |

## Sécurité

- Aucun secret dans le code : tout passe par `.env`, ignoré par Git.
- Le bot n'a pas besoin de la permission Administrateur ni d'aucun intent privilégié.
- Il ne crée, ne renomme et ne supprime aucun salon, rôle ou message d'utilisateur.
- Les commandes d'administration sont protégées deux fois : permission par défaut
  côté Discord et vérification côté bot.

## Documentation

- [Architecture, flux de données et plan V1 à V5](docs/architecture.md)
- [Guide d'installation Windows](docs/installation-windows.md)

## Tests

```powershell
python -m pytest
```

Les tests n'utilisent ni token, ni réseau, ni vrai serveur Discord : Discord est
remplacé par une fausse implémentation en mémoire.
