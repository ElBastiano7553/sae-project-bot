# Rapport de travail autonome : Samae 1.14.0

Mission lancée par Romain le 27/11/2026 (« MISSION AUTONOME — SAMAE »). Travail sur la branche
`claude/project-thread-vcge2n`, proposé dans une pull request **non fusionnée**. Rien n'a été déployé.

## En bref

| | État |
|---|---|
| Ce qui fonctionne et est testé | idées, tier lists, débats, compte rendu enrichi, suivi Excel (sur un classeur de **test**), `/samae`, migration v15 : **1154 tests verts** sur Python 3.11, 3.12 et 3.13 (1100 avant la mission) |
| Ce qui n'est pas testé sur Discord | tout le nouveau code : les tests utilisent de faux objets Discord ; le plan de test manuel est dans [docs/test-manuel.md](docs/test-manuel.md#1140--idées-tier-lists-débats-suivi-excel-samae) |
| Ce qui n'est pas déployé | 1.14.0 (le VPS n'était pas joignable ; et Romain fusionne et déploie lui-même) |
| Ce qui est bloqué | le vrai classeur « GAME DESIGN DATABASE — LUNASTEH.xlsx » n'a pas pu être lu (introuvable dans l'environnement, PC non connecté) ; le plugin Discord de Claude Code n'est pas configuré (il faut un token et le PC de Romain) |
| Frais engagés | aucun : aucun abonnement, aucune clé d'API créée |

## 1. Audit (début de mission)

- Dépôt en 1.13.0 : discord.py 2.7.1, SQLite schéma v14, 1100 tests verts, architecture en couches
  (services sans discord.py, SQL dans les dépôts, cogs minces), déploiement systemd prêt.
- Points d'attention relevés lors de l'audit d'octobre et toujours ouverts (non traités ici, hors
  périmètre) : erreur passagère sur `channel_guild_id` ignorée, `restore_pending` après démarrage du
  serveur, renvoi possible si `close()` survient pendant un envoi, limite de débit GitHub B4 affichée
  comme refus d'accès, GitHub ne renvoie jamais automatiquement un webhook perdu.
- Ce qui manquait par rapport à la mission : idées et tier lists, débats, suivi du classeur, assistant
  conversationnel. Tout a été ajouté sans toucher aux fonctions existantes.

## 2. Skills

Détail : [docs/skills.md](docs/skills.md) et [.claude/skills/SOURCES.md](.claude/skills/SOURCES.md).

| Candidat | Décision |
|---|---|
| `discord-bot-architect` (sickn33/agentic-awesome-skills, commit 560a2b0, MIT / CC BY 4.0) | installé (Markdown seulement ; exemples en Pycord, les règles du projet priment) |
| `python-testing-patterns` (même dépôt) | installé |
| `samae-project` | écrit pour ce dépôt (architecture, conventions, règles de l'équipe) ; proposé aussi comme Skill personnel |
| Nice-Wolf-Studio/agent-discord-skills | écarté : un seul commit, token dans des commandes `curl`, suppression de salons, redondant |
| `discord-automation` (sickn33) | écarté : passe par un service tiers (Composio) |
| `xlsx` (sickn33) | écarté : dossier vide ; le Skill intégré suffit |
| plugin officiel `discord` | inspecté et testé à blanc (voir 3) ; s'installe sur le PC, pas dans le dépôt |

## 3. Claude et Discord

**Usage A, parler à Claude Code depuis le téléphone** : guide Windows complet
[docs/claude-code-discord.md](docs/claude-code-discord.md) et script
`deploy/claude-discord/lancer-claude-discord.ps1`. Vérifié : le plugin exige Bun, s'installe, refuse de
démarrer sans token ; les « channels » sont une préversion qui ne tourne que tant que la session est
ouverte. Accès limité par appairage puis politique `allowlist`. **Non fait** : création du bot dédié,
token, appairage (demandent Romain et son PC).

**Usage B, Samae assistant** : comparaison API / MCP / intermédiaire dans
[docs/assistant.md](docs/assistant.md). Retenu : l'intermédiaire. Les demandes courantes (bilan,
fonctionnalités restantes, décisions, préparer une réunion…) sont traitées sans IA, gratuitement ;
seules les questions libres vont à Claude, **si** `AI_PROVIDER=anthropic` est configuré. Coût estimé
d'une question libre : environ 0,022 $ avec Opus 5.5, 0,0006 $ avec Haiku 5.5.

## 4. Suivi du classeur Excel

Détail : [docs/suivi-excel.md](docs/suivi-excel.md).

- Une seule source de vérité : la base de Samae après import ; le classeur original n'est **jamais**
  modifié (copie gardée dans `data/tracker/originals/`), l'export écrit une nouvelle copie.
- Analyse automatique de la structure (en-têtes, identifiants, statuts, listes déroulantes, formules,
  références du DASHBOARD) : rien n'est supposé sur les colonnes du vrai fichier.
- Formules jamais écrasées ; recopiées sur les nouvelles lignes ; Excel recalcule à l'ouverture.
- Sauvegarde de la base avant chaque import et chaque validation.
- Une décision officielle n'est jamais ajoutée ni modifiée sans validation par un administrateur.
- **Aucune donnée inventée** : le classeur de test `tests/tracker_fixture.py` sert uniquement aux tests
  et le dit.
- Limite : openpyxl ne recopie pas les graphiques et images dans l'export (l'original reste intact).

## 5. Meeting Manager

Détail : [docs/meeting-manager.md](docs/meeting-manager.md). `/idee` (formulaire, 5 idées en attente par
membre, idées proches, familles, catalogue), `/tierlist` (S=6 … F=1, votes masqués et modifiables
jusqu'à validation, moyenne, médiane, écart type, égalités, avis extrêmes anonymes, désaccords ; une
idée classée F n'est jamais éliminée), `/debat` (arguments, objections, vote de départage, décision
proposée par un membre et confirmée par un admin), et le compte rendu de `/reunion terminer` reprend
tier lists, idées retenues et écartées, décisions de débat et questions ouvertes. Une décision de débat
confirmée est ajoutée aux DECISIONS du suivi.

## 6. Assistant `/samae`

Reconnaît les exemples de la mission (bilan, fonctionnalités restantes, dernières décisions, préparer
la réunion, idées en attente, résumé de la semaine, ajouter une idée, questions ouvertes, recherche).
« Ajoute cette idée » ouvre un formulaire prérempli : rien n'est enregistré sans validation. Limite :
Samae ne lit pas les messages des salons, donc le résumé de la semaine porte sur ce qui est enregistré
dans Samae.

## 7. Tests

| Python | Résultat |
|---|---|
| 3.11.18 | 1154 réussis |
| 3.12.3 | 1154 réussis |
| 3.13.18 | 1154 réussis |

54 nouveaux tests : `test_ideas.py` (calculs, limites, votes masqués, débats, migration v14 → v15 avec
sauvegarde), `test_tracker.py` (analyse, import sans suppression, propositions, conflits, export qui
préserve les formules et l'original), `test_project_assistant.py` (intentions, rien n'est écrit sans
validation, IA facultative), `test_ideas_commands.py` (commandes avec un faux Discord), plus les tests
de l'aide et des commandes mis à jour.

## 8. Déploiement

Non déployé. Préparé : la mise à jour passe par `deploy/update.sh` habituel (sauvegarde vérifiée,
installation d'openpyxl, migration v15 additive avec sauvegarde `…-avantv15`, retour arrière par
`rollback.sh`). Les nouveaux dossiers sont sous `data/` et `<BACKUP_DIR>`, déjà autorisés par systemd.
Aucune variable obligatoire en plus. Note ajoutée dans [docs/deployment-vps.md](docs/deployment-vps.md).

## 9. Fichiers

- Nouveaux : `src/database/ideas.py`, `src/database/tracker.py`, `src/services/ideas.py`,
  `src/services/tracker.py`, `src/services/project_assistant.py`, `src/tracker/` (analyse, lecture,
  export, outil `python -m src.tracker.inspect`), `src/discord_bot/ideas_ui.py`,
  `src/discord_bot/cogs/ideas.py`, `cogs/tracker.py`, `cogs/assistant.py`, `.claude/skills/`,
  `deploy/claude-discord/lancer-claude-discord.ps1`, les docs `meeting-manager.md`, `suivi-excel.md`,
  `assistant.md`, `claude-code-discord.md`, `skills.md`, et 5 fichiers de tests.
- Modifiés : `connection.py` (migration v15), `meetings.py` (sections ajoutées au compte rendu),
  `ai_assistant.py` (question libre sur le projet), `config.py` (`ASSISTANT_ON_MENTION`), `bot.py`,
  `help.py`, README, `commands.md`, `architecture.md`, `deployment-vps.md`, `test-manuel.md`,
  `.env.example`, version 1.14.0.
- Dépendances ajoutées : `openpyxl==3.1.5` et `et-xmlfile==2.0.0` (MIT).

## 10. Ce qu'il reste à faire, et qui

À faire par Romain :

1. Relire et fusionner la pull request, puis `bash deploy/update.sh` sur le VPS.
2. Analyser le vrai classeur sur le PC, sans le modifier :
   `python -m src.tracker.inspect "C:\chemin\GAME DESIGN DATABASE — LUNASTEH.xlsx" --markdown analyse.md`,
   puis `/suivi importer` sur Discord.
3. Dérouler le plan de test 1.14.0 sur un serveur de test.
4. Facultatif : créer le bot dédié et suivre [docs/claude-code-discord.md](docs/claude-code-discord.md).
5. Facultatif : décider d'activer l'IA (clé Anthropic avec plafond de dépense, `AI_MODEL=claude-haiku-5-5`
   conseillé).

Encore à développer : sondages et ressources (Samae 2.0 lot 3), multi-projets B6 et B7, et les points de
l'audit d'octobre cités en 1.
