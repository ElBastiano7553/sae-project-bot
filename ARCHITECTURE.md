# Architecture de Samae (1.15.0)

Vue d'ensemble à jour. L'historique détaillé de la conception (lots, flux de la V1 et de la V2) reste
dans [docs/architecture.md](docs/architecture.md) ; les guides par domaine sont dans [docs/](docs/).

## 1. Principes

1. **Séparation métier / Discord.** La logique est dans `src/services/` et ne dépend jamais de
   discord.py : elle se teste sans Discord. Les cogs ne font qu'identifier la personne, appeler le
   service et répondre.
2. **SQL à un seul endroit** (`src/database/`), migrations versionnées **uniquement additives**,
   sauvegarde automatique avant chaque migration.
3. **Une panne extérieure ne fait pas tomber le bot.** Chaque intégration (GitLab, GitHub, projets,
   IA, Google Sheets) démarre dans un bloc protégé ; son état apparaît dans `/etat`.
4. **Rien d'important sans un humain.** Décisions, changements du suivi, refonte du serveur : aperçu
   puis confirmation d'un administrateur. Le classeur de l'équipe n'est jamais modifié.
5. **Secrets hors du code et des journaux** (`.env`, `SECRET_ENV_VARS`, masquage dans les journaux),
   aucun intent privilégié, aucune mention qui notifie.

## 2. Couches et modules

```
src/
├── main.py                 Point d'entrée : .env, configuration, verrou d'instance, journaux, lancement
├── config/                 settings.py (.env → Settings), files.py (config/*.toml), base.py (chemins, ConfigError)
├── notifications.py        Socle des files de notifications : statuts, essais, arrêt propre (1.15.0)
├── logging_setup.py, instance_lock.py, systemd_notify.py, healthcheck.py, ops.py   Exploitation
├── database/               SQLite : connection.py (migrations v1 à v16) + un dépôt par domaine
├── services/               Métier sans discord.py (réunions, idées, suivi, projets, refonte, aide, IA…)
├── gitlab/                 Webhooks GitLab (serveur, sécurité, mise en forme, file), API en lecture
├── github/                 Webhooks GitHub du jeu, lecture du dépôt pour la synchronisation (1.15.0)
├── providers/              Réception commune des projets (/projet), GitHub App, OAuth GitLab
├── tracker/                Classeur de suivi : lecture, analyse, vérification, Google Sheet, export
├── ai/                     Fournisseurs d'IA (aucun, Anthropic, faux pour les tests)
└── discord_bot/            bot.py (cycle de vie, intégrations), cogs/ (commandes), *_ui.py et embeds.py (affichage)
```

| Paquet | Dépend de | Ne dépend jamais de |
|---|---|---|
| `config`, `database`, `services`, `tracker`, `gitlab`, `github`, `providers`, `ai`, `notifications` | bibliothèque standard, aiohttp, openpyxl, entre eux | discord.py |
| `discord_bot` | tout le reste | — |

Taille : 116 fichiers Python dans `src/` (28 000 lignes environ, 1.14.0 et 1.15.0 comprises), 62 fichiers de tests.

## 3. Flux principaux

**Démarrage** (`SaeBot.setup_hook`, `on_ready`) : base ouverte et migrée, cogs chargés, commandes
synchronisées, serveurs HTTP des webhooks démarrés (chacun protégé), notifications non envoyées
remises en file **avant** l'ouverture des ports, boucle de maintenance (tableau de bord, purge de
l'historique, synchronisation GitHub facultative).

**Notification** : webhook signé → vérification (secret, dépôt ou projet autorisé, taille) → mise en
forme → enregistrement avec anti-doublon → réponse immédiate au fournisseur → envoi Discord par une
`NotificationQueue` (nouveaux essais, refus définitif reconnu). À l'arrêt : ports fermés, puis l'envoi
en cours peut finir.

**Suivi du projet** : classeur importé (copie gardée telle quelle) → lignes en base → consultation et
propositions → validation d'un admin → export dans une nouvelle copie. `/suivi verifier` contrôle le
Google Sheet ou un fichier sans rien écrire. `/suivi synchroniser` relie les commits et PR GitHub aux
lignes citées et propose des statuts.

## 4. Cartographie des fonctionnalités (28/11/2026)

« Testé » veut dire tests automatiques avec un faux Discord. Ce qui est en service sur le VPS n'a pas
été vérifié dans cette mission (plusieurs versions fusionnées n'étaient pas encore déployées).

| Domaine | Commandes | État |
|---|---|---|
| Consultation | `/aide`, `/etat` | Fonctionnel |
| Installation et administration | `/setup`, `/admin` (11 sous-commandes) | Fonctionnel |
| Messages permanents | `/admin installer`, `actualiser`, `reconstruire`, `messages` | Fonctionnel |
| Refonte du serveur | `/refonte lancer`, `retour`, `journal` | Fonctionnel (aperçu, retour arrière) |
| Accueil et rôles | `/roles`, message d'arrivée | Fonctionnel ; l'accueil demande l'intent « membres » |
| Réunions | `/reunion` (16 sous-commandes) | Fonctionnel |
| Notifications GitLab et GitHub (webhooks) | automatique | Fonctionnel ; GitLab encore à valider avec le vrai GitLab de l'école selon la doc |
| Activité des dépôts | `/activite resume`, `evenements`, `stats`, `contributions` | Fonctionnel (données GitLab) |
| Projets suivis | `/projet` (6 sous-commandes), GitHub App, OAuth GitLab | Partiellement : demande la GitHub App et l'application GitLab configurées sur le serveur |
| IA | `/ia question`, `bilan`, `relecture` | Partiellement : facultatif (clé), `bilan` et `relecture` ne couvrent que GitLab |
| Idées, tier lists, débats | `/idee`, `/tierlist`, `/debat` (27) | À valider sur Discord (1.14.0, PR #26 ouverte) |
| Suivi du projet | `/suivi` (19), `/samae` | À valider sur Discord ; import vérifié sur la structure réelle du classeur |
| Vérification du classeur | `/suivi verifier`, `python -m src.tracker.verify` | Fonctionnel hors Discord (1.15.0), vérifié sur le vrai Google Sheet |
| Synchronisation GitHub | `/suivi synchroniser`, `/suivi code` | À valider avec le vrai dépôt (1.15.0, testé avec un faux GitHub) |
| Musique | — | Retirée en 1.13.0 (il reste `LAVALINK_PASSWORD` dans la liste des secrets masqués, sans effet) |
| Exploitation | sauvegardes, verrou d'instance, systemd, contrôle périodique, Docker | Fonctionnel |

## 5. Décisions techniques

| Décision | Raison |
|---|---|
| discord.py 2.7.1 plutôt que Pycord | Maintenu, commandes slash natives ; tout le code et les tests l'utilisent |
| SQLite (WAL) plutôt qu'un serveur de base | 5 utilisateurs, un seul processus, sauvegarde par simple copie |
| Migrations additives seulement | Retour arrière par restauration de la sauvegarde d'avant migration, jamais de perte |
| Webhooks + file en mémoire + base | Réponse en moins de 10 s exigée par GitLab et GitHub ; rien de perdu au redémarrage |
| `src/notifications.py` commun (1.15.0) | Les trois files recopiaient la même boucle et GitHub dépendait de GitLab |
| `src/config/` en paquet (1.15.0) | Le fichier unique mêlait `.env` et TOML (1168 lignes) ; importations inchangées |
| Google Sheet lu par son lien de partage | Aucune clé d'API, aucun droit d'écriture, rien à configurer côté Google |
| Synchronisation GitHub = propositions | Le classeur reste sous le contrôle de l'équipe ; pas de doublon avec les webhooks |
| Pas de restructuration en `core/`, `integrations/` | Les paquets actuels suivent déjà la séparation métier / données / Discord ; déplacer une centaine de fichiers casserait l'historique Git et les PR ouvertes sans bénéfice |

## 6. Dépendances

Exécution : discord.py, aiohttp, python-dotenv, anthropic (seulement si `AI_PROVIDER=anthropic`),
openpyxl et et-xmlfile (suivi Excel). Développement : pytest, pytest-asyncio, ruff. Toutes épinglées ;
`pip-audit -r requirements.txt` : aucune vulnérabilité connue au 28/11/2026.
