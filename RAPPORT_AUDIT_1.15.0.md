# Rapport final : audit, restructuration et fondation de Samae (1.15.0)

Mission du 28/11/2026, faite en autonomie sur le PC de Romain. Travail sur la branche
**`claude/socle-1.15`**, poussée sur GitHub, **sans pull request ouverte** et **rien de déployé**.

## En bref

| | État |
|---|---|
| Projet local | `D:\Desktop\claude\samae`, clone complet avec tout l'historique Git ; sauvegarde bundle avant toute modification |
| Socle | Files de notifications regroupées, configuration découpée, 4 défauts de l'audit d'octobre et 2 défauts du suivi corrigés, ruff dans la CI |
| Outils demandés ensuite | Vérification du classeur (`/suivi verifier`) et synchronisation avec le dépôt GitHub (`/suivi synchroniser`, `/suivi code`) |
| Skills | 4 skills Samae créés, 1 skill externe ajouté (`async-python-patterns`), 2 déjà présents conservés ; plugin Discord non installable ici (Bun et token absents) |
| Documentation | `ARCHITECTURE.md`, `DEVELOPMENT.md`, `CHANGELOG.md`, `ROADMAP.md`, README complété |
| Tests | **1213 réussis**, 4 ignorés, sur Python 3.13 (1096 sur `main` au départ, 1150 avec la PR #26) ; ruff propre |
| Pas fait | Rien testé sur un vrai serveur Discord ni sur le VPS ; Python 3.11 et 3.12 seront testés par la CI à l'ouverture de la PR |

## A. État initial

- **Version de référence** : dépôt GitHub `ElBastiano7553/sae-project-bot`, `main` = 1.13.0 (commit `16d90b3`).
  La **PR #26 (1.14.0 : idées, tier lists, débats, suivi Excel, `/samae`) est ouverte**, non fusionnée.
- **Architecture** : en couches et saine (métier sans discord.py dans `src/services/`, SQL dans
  `src/database/`, cogs minces, migrations additives v1 à v14), hébergement VPS systemd prêt, Docker vérifié par la CI.
- **Taille** : 94 fichiers Python dans `src/` et 53 fichiers de tests sur `main` ; 1096 tests.
- **Fonctionnalités** : 93 commandes (cartographie complète dans `ARCHITECTURE.md`, section 4).
- **Problèmes identifiés** :
  - trois files de notifications recopiées (GitHub et projets dépendaient du module GitLab) ;
  - arrêt pendant un envoi : notification **renvoyée en double** au redémarrage ;
  - remise en file au démarrage après l'ouverture du port : doublon possible ;
  - erreur passagère de Discord sur le salon d'un projet : notification abandonnée à tort ;
  - limite de débit GitHub affichée comme « accès refusé » ;
  - `src/config.py` de 1168 lignes mêlant `.env` et TOML ;
  - 1.14.0 : VALIDÉ compté comme « terminé », ABANDONNÉ compté comme « restant » ;
  - 4 imports inutiles, 1 variable inutile, aucun contrôle statique dans la CI ;
  - documentation d'architecture datée (schéma v9, étapes déjà faites présentées comme à venir).

## B. Transfert local

- **Emplacement final** : `D:\Desktop\claude\samae` (le dossier était vide). Clone complet : toutes les
  branches et l'historique depuis le premier commit.
- **État Git** : branche `claude/socle-1.15`, partie de la branche de la PR #26
  (`claude/project-thread-vcge2n`), poussée sur `origin`. Rien d'autre n'a été poussé.
- **Comparaison local / distant** : trois anciennes copies dans `D:\Documents\sae-project-bot-v1\`
  (1.11.0 sur une branche `pr-23-music`, une copie 1.12 et une copie sans Git). Elles sont plus
  anciennes que GitHub et ne contiennent pas de travail absent du dépôt (seulement des fichiers
  temporaires de tests et un `.egg-info`). **Je n'y ai pas touché.** Elles contiennent des fichiers
  `.env` avec des secrets : à supprimer quand tu le décideras.
- **Fichiers ignorés et secrets** : `.env`, `.env.*` (sauf l'exemple), `data/`, `logs/`, `.venv/` sont
  ignorés ; j'ai ajouté `*.egg-info/` et les dossiers temporaires des tests. Aucun secret dans le dépôt.
  Je n'ai lu aucun `.env`.
- **Sauvegarde** : `D:\Desktop\claude\samae-sauvegardes\sae-project-bot-<date>.bundle` (toutes les
  branches de GitHub, restaurable avec `git clone <fichier>.bundle`).
- **Dépendances** : venv `.venv` (Python 3.13) avec `requirements-dev.txt` ; `pip-audit` : aucune
  vulnérabilité connue dans les versions épinglées.

## C. Skills et outils

Inventaire de départ : skills du dépôt `discord-bot-architect`, `python-testing-patterns` (sickn33,
commit `560a2b0`), `samae-project` ; plugin `cloudflare` seul installé ; catalogue officiel
`claude-plugins-official` déjà connu de Claude Code.

| Élément | Décision | Lien |
|---|---|---|
| `discord-bot-architect` | conservé (Markdown seulement ; exemples Pycord, les skills Samae priment) | https://github.com/sickn33/agentic-awesome-skills |
| `python-testing-patterns` | conservé | même dépôt |
| `async-python-patterns` | **installé** (lu en entier, Markdown seulement, MIT / CC BY 4.0, commit `ee8ccaa`) | même dépôt |
| `python-pro` | refusé : marqué `risk: critical`, vise 3.12+, FastAPI, Django : hors de notre pile | même dépôt |
| `security-audit-static` | refusé : sain mais redondant avec `/security-review`, et coûteux (sous-agents sur le modèle le plus cher) | même dépôt |
| `code-review-*`, `git-*`, `github-*` (des dizaines) | refusés : redondants avec les outils intégrés (`/code-review`, `/security-review`, `/simplify`, `git`) | même dépôt |
| Excel | outil intégré `anthropic-skills:xlsx` + skill `samae-game-design` ; le `xlsx` du catalogue est vide | — |
| Plugin `discord` | **non installé** : il exige Bun (absent du PC) et un bot Discord dédié avec son token et l'intent « Message Content », à créer par toi. Guide prêt : `docs/claude-code-discord.md`. Il ne marche que pendant une session Claude Code ouverte, et reste distinct de Samae | https://github.com/anthropics/claude-plugins-official |
| Plugin `security-guidance` | non activé d'office : relecture par un modèle à chaque fin de tour et à chaque commit (consomme l'abonnement), `pip install` automatiques | même dépôt |
| Plugin `pyright-lsp` | recommandé, facultatif (demande `npm install -g pyright`) | même dépôt |
| Plugin `github` | non installé : demande un jeton personnel, `git` suffit | même dépôt |

**Skills Samae créés** (`.claude/skills/`) : `samae-project-context` (remplace `samae-project`, mis à
jour pour 1.15.0), `samae-game-design` (structure réelle du classeur, règles, vérification),
`samae-discord-ux` (commandes, embeds, composants, erreurs, confirmations), `samae-quality-assurance`
(tests à écrire, vérifications, revue, compte rendu honnête). Leur en-tête (`name`, `description`) est valide ;
la détection est à confirmer avec `/skills` dans une nouvelle session. Provenance : `.claude/skills/SOURCES.md` ; décisions : `docs/skills.md`.

## D. Modifications

**Architecture retenue** : je n'ai pas appliqué la structure `core/`, `cogs/`, `integrations/`
proposée. Les paquets actuels assurent déjà la séparation demandée (métier, données, intégrations,
Discord) ; déplacer une centaine de fichiers aurait cassé l'historique Git et la PR #26 ouverte sans
bénéfice. J'ai corrigé les vrais problèmes de structure :

| Amélioration | Fichiers |
|---|---|
| Socle commun des notifications (`NotificationQueue`, essais, arrêt propre) | `src/notifications.py` (nouveau), `src/gitlab/dispatcher.py`, `src/github/dispatcher.py`, `src/providers/dispatcher.py` |
| Arrêt propre, remise en file avant l'ouverture des ports, salon vérifié avec nouvel essai | `src/discord_bot/bot.py` |
| Limite de débit GitHub reconnue | `src/providers/github_app.py` |
| Configuration en paquet (`settings.py`, `files.py`, `base.py`), importations inchangées | `src/config/` (remplace `src/config.py`) |
| VALIDÉ n'est plus « terminé », ABANDONNÉ n'est plus « restant », statuts de référence du classeur | `src/services/tracker.py` |
| Vérification du classeur | `src/tracker/checks.py`, `src/tracker/google.py`, `src/tracker/verify.py` (nouveaux), `cogs/tracker.py` |
| Synchronisation avec le dépôt GitHub, migration v16 | `src/github/repo_sync.py`, `src/services/repo_sync.py`, `src/database/repo_sync.py` (nouveaux), `connection.py`, `cogs/tracker.py` |
| UX : autocomplétion de `/suivi code`, erreurs qui disent quoi faire, rapport complet joint en .md | `cogs/tracker.py` |
| Contrôle statique dans la CI (ruff F, E9, B) | `pyproject.toml`, `requirements-dev.txt`, `.github/workflows/tests.yml` |
| Réglages `TRACKER_GOOGLE_SHEET`, `GITHUB_SYNC_*` (jeton masqué) | `src/config/settings.py`, `.env.example` |
| Documentation | `ARCHITECTURE.md`, `DEVELOPMENT.md`, `CHANGELOG.md`, `ROADMAP.md`, ce rapport (nouveaux) ; README, `docs/commands.md`, `suivi-excel.md`, `synchronisation-github.md` (nouveau), `test-manuel.md`, `deployment-vps.md`, `architecture.md`, `skills.md` |

**Revue UX des commandes** : les 93 commandes suivent déjà des conventions cohérentes (groupes par
domaine, réponses éphémères, confirmations pour les actions sensibles, erreurs expliquées, aide
générée et testée). Je n'ai donc rien renommé : renommer des commandes que l'équipe connaît coûterait
plus que cela n'apporte. Points notés pour plus tard : `/reunion` (16 sous-commandes) et le trio
`/idee`, `/tierlist`, `/debat` (27) sont denses ; `/ia bilan` et `/ia relecture` ne couvrent que GitLab
(voir `ROADMAP.md`).

**Vérification du vrai Google Sheet** (lu avec le connecteur Google Sheets) : aucune erreur, DASHBOARD
exact, CHANGELOG complet, aucun renvoi cassé ; **5 avertissements** : FEAT-002, 003, 004, 007 et 008
sont VALIDÉ sans décision dans DECISIONS. C'est la question Q-014 du classeur. Je l'ai vérifié sur une
transcription des identifiants, statuts, dates et renvois, pas sur un export du classeur complet.

**Synchronisation GitHub**, telle que je l'ai comprise : relier le code du jeu au classeur. Samae
relit les commits et les PR, repère les FEAT-xxx cités et **propose** EN COURS ou TERMINÉ. Un admin
valide ; rien n'est écrit sur GitHub ni dans le classeur. Si tu pensais à autre chose (mettre à jour le
bot sur le serveur à partir de GitHub, par exemple), le moteur de lecture du dépôt est réutilisable.

## E. Tests

| Étape | Résultat |
|---|---|
| `main` (1.13.0) au départ | 1096 réussis, 4 ignorés |
| Branche de la PR #26 (1.14.0) | 1150 réussis, 4 ignorés |
| Branche `claude/socle-1.15`, après chaque étape et à la fin | **1213 réussis, 4 ignorés**, aucune régression |
| `ruff check src tests` | propre |
| Import sans configuration (`src.main`, `src.discord_bot.bot`) | OK |
| `pip-audit -r requirements.txt` | aucune vulnérabilité connue |

63 tests ajoutés : `test_socle.py` (arrêt propre, erreur passagère, ordre de démarrage, limite de
débit), `test_tracker_checks.py` (chaque contrôle, Google, ligne de commande, réglage),
`test_repo_sync.py` (faux GitHub : propositions, pas de doublon, PR fusionnée, erreurs), tests de
commandes avec un faux Discord. Deux tests existants rendus indépendants du numéro de migration.

**Reste à vérifier** : tout sur un vrai serveur Discord (`docs/test-manuel.md`, sections 1.14.0 et
1.15.0) ; Python 3.11 et 3.12 (CI) ; la lecture du Google Sheet depuis le VPS ; la synchronisation avec
le vrai dépôt du jeu.

## F. État final

- **Opérationnel (tests automatiques)** : tout l'existant, plus la vérification du classeur et la
  synchronisation GitHub. Cartographie par domaine : `ARCHITECTURE.md`, section 4.
- **À améliorer** : `/ia` limité à GitLab ; commandes denses (`/reunion`, idées) ; 54 blocs d'imports
  non triés et lignes longues (style, non imposé par la CI) ; `docs/architecture.md` reste un
  historique (la vue à jour est `ARCHITECTURE.md`).
- **Problèmes encore présents** : GitHub ne renvoie jamais un webhook perdu (la synchronisation
  enregistre le code manqué mais ne republie pas les notifications, volontairement, pour éviter les
  doublons) ; `LAVALINK_PASSWORD` reste dans la liste des secrets masqués (sans effet).
- **Prérequis manquants** (à fournir par toi) : fusion de #26 puis de cette branche ; déploiement ;
  Google Sheet partagé par lien et `TRACKER_GOOGLE_SHEET` ; `GITHUB_SYNC_REPOSITORY` (et un jeton en
  lecture seule si le dépôt est privé) ; pour le plugin Discord de Claude Code, Bun et un bot dédié.

## G. Feuille de route

Détail (objectif, fonctionnement, modules, difficultés, tests, contraintes Discord) dans `ROADMAP.md` :

0. Mettre le socle en service (fusion, déploiement, tests manuels, Q-014).
1. Priorité 1 : résumé des modifications du code GitHub par l'IA ; tableau d'avancement du projet
   dans un salon ; rappel aux admins des propositions en attente.
2. Priorité 2 : déconnexion d'un projet (B6) ; sondages ; ressources partagées.
3. Priorité 3, à décider : écrire les validations dans le Google Sheet (change une règle de l'équipe) ;
   parler à Claude Code depuis Discord (plugin, sans code dans Samae).

Architecture proposée pour ces ajouts : mêmes couches (service sans discord.py, dépôt SQL, migration
additive, cog mince, `HelpEntry`), et réutilisation des briques existantes (`repo_sync_items` pour le
code GitHub, messages permanents pour le tableau d'avancement, `NotificationQueue` pour les envois).

## À faire par Romain

1. Relire et fusionner la PR #26, puis ouvrir et fusionner la PR de `claude/socle-1.15`
   (https://github.com/ElBastiano7553/sae-project-bot/pull/new/claude/socle-1.15).
2. Sur le VPS : `bash deploy/update.sh` (migrations v15 puis v16, sauvegardes automatiques).
3. Dans `.env` : `TRACKER_GOOGLE_SHEET`, `GITHUB_SYNC_REPOSITORY=ElBastiano7553/game-project`, et si besoin
   `GITHUB_SYNC_TOKEN` (lecture seule) et `GITHUB_SYNC_INTERVAL_MINUTES`.
4. Dérouler `docs/test-manuel.md` (1.14.0 et 1.15.0) sur un serveur de test.
5. Trancher Q-014 dans le classeur.
