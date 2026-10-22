# Samae 2.0 : audit et plan d'implémentation

Ce document décrit l'état du dépôt avant Samae 2.0, puis le plan retenu pour les
quatre lots (réunions, idées et tier lists, débat et communauté, finitions).
Seul le lot 1 est implémenté à ce stade (version 1.1.0, 442 tests).

## 1. Audit initial (22/10/2026)

| Point vérifié | Constat |
|---|---|
| Branche et état Git | `main` à jour (`2c32d59`, fusion de la PR #4), working tree propre. Travail de Samae 2.0 sur la branche locale `claude/project-thread-k6i659`. |
| PR ouvertes | Aucune (PR #1 à #4 fusionnées). |
| Version | 1.0.0, schéma SQLite v6. |
| Tests initiaux | `python -m pytest -q` (Python 3.13.16) : **399 passed** en 4,3 s. |
| Dépendances | discord.py 2.7.1, aiohttp 3.14.5, python-dotenv 1.2.4, anthropic 1.12.1. Aucune nouvelle dépendance n'est nécessaire pour Samae 2.0. |
| Architecture | Logique métier dans `src/services/` (sans discord.py), SQL uniquement dans `src/database/`, adaptateurs Discord dans `src/discord_bot/` (cogs, embeds, vues). |
| Migrations | Liste `MIGRATIONS` + `PRAGMA user_version`, appliquées dans une transaction, sauvegarde vérifiée avant toute migration d'une base existante. Uniquement des ajouts depuis la v1. |
| Permissions | `is_bot_admin` : « Gérer le serveur », « Administrateur » ou rôle `ADMIN_ROLE_NAME`. Commandes d'administration protégées deux fois (permission par défaut + vérification côté bot). |
| Intents | Aucun intent privilégié (ni membres, ni contenu des messages). Conséquence : les participants sont désignés par mention ou par les options « membre » de Discord, jamais par recherche dans la liste des membres. |
| Mentions | `AllowedMentions.none()` partout : une mention affichée dans un panneau ne notifie personne. |
| Embeds | `make_embed` / `add_field` / `fit_embed` et `services/design.py` (couleurs, icônes, horodatages, limites dans `text_limits.py`). |
| Vues | `ConfirmView` et `AnnouncementModal`, non persistantes. Aucune vue persistante avant Samae 2.0. |
| Aide | `services/help.py` : un test impose que l'aide corresponde exactement aux commandes enregistrées. |
| Nom affiché | Le code s'appelle encore « SAE Project Bot » (pieds de page, marqueur des messages permanents). Le renommage en Samae est prévu au lot 4, car le pied de page sert à retrouver les messages permanents. |

## 2. Décisions d'architecture

- **Pas de réorganisation du dépôt.** Chaque domaine ajoute un dépôt SQL
  (`src/database/<domaine>.py`), un service métier testable sans Discord
  (`src/services/<domaine>.py`), un cog (`src/discord_bot/cogs/<domaine>.py`) et
  ses embeds et vues (`src/discord_bot/<domaine>_ui.py`).
- **Les permissions sont décidées dans les services**, qui reçoivent un `Actor`
  (identifiant, nom, « admin du bot » ou non). Les boutons masqués ne sont qu'un
  confort : chaque bouton et chaque commande revérifie côté serveur.
- **Boutons persistants** : `discord.ui.DynamicItem`, dont l'identifiant contient
  l'objet visé (`samae:mtg:<id>:<action>:<révision>`). Ils fonctionnent après un
  redémarrage sans rien enregistrer en mémoire.
- **Révision optimiste** : chaque réunion a un compteur `revision`. Les boutons qui
  font avancer la réunion portent la révision du panneau ; un clic sur un panneau
  périmé (double clic, deux personnes en même temps) est refusé et le panneau est
  actualisé, au lieu de sauter deux étapes.
- **Transactions** : toute opération qui écrit plusieurs lignes (création, passage
  d'étape, clôture) est faite dans une seule transaction, avec des `UPDATE … WHERE`
  conditionnels qui vérifient l'état attendu.
- **Un panneau = un message** : le panneau est modifié sur place ; s'il a été
  supprimé, il est republié une seule fois et son nouvel identifiant remplace l'ancien.
- **Pas d'IA** dans l'intégrité des votes, des plafonds et des permissions.

## 3. Tables SQLite prévues

| Lot | Migration | Tables |
|---|---|---|
| 1 | v7 | `meetings`, `meeting_participants`, `meeting_steps`, `meeting_notes` (décisions, réserves, désaccords), `meeting_actions`, `meeting_log` (historique) |
| 2 | v8 | `ideas`, `idea_tags`, `idea_history`, `idea_merges`, `meeting_ideas`, `rankings`, `ranking_items`, `ranking_voters`, `ranking_votes` (unique par votant et idée) |
| 3 | v9 | `ballots` (départages), `ballot_options`, `ballot_votes`, `polls`, `poll_options`, `poll_votes`, `self_roles`, `role_panels`, `resources` |
| 4 | — | Aucune table prévue ; vérification des migrations v7 à v9 sur une copie de base réelle. |

Le détail des colonnes du lot 1 est dans [meetings.md](meetings.md).

## 4. Commandes Discord prévues

| Lot | Commandes |
|---|---|
| 1 | `/reunion creer`, `voir`, `liste`, `rejoindre`, `quitter`, `inviter`, `demarrer`, `suivante`, `precedente`, `progression`, `point`, `decision`, `reserve`, `action`, `terminer`, `annuler`, `compte-rendu` (+ boutons du panneau) |
| 2 | `/idee proposer`, `voir`, `chercher`, `modifier`, `retirer`, `associer`, `fusionner` ; `/reunion classer` (+ boutons S à F) et `/classement …` (progression, ma participation, clôture, résultats) |
| 3 | `/debat sujets`, `/debat departage`, `/debat decider` ; `/sondage creer`, `cloturer` ; `/roles panneau`, `autoriser`, `retirer` ; `/ressources ajouter`, `chercher`, `modifier`, `retirer` |
| 4 | Aucune nouvelle commande : harmonisation, pagination, erreurs, documentation. |

## 5. Plan par lot

1. **Fondations et réunions** (✅ livré, voir [meetings.md](meetings.md)) : migration v7, dépôt, service, panneau
   persistant, compte rendu paginé, tests, documentation.
2. **Idées et tier lists** : catalogue, plafond de cinq idées actives vérifié dans
   la transaction d'insertion, recherche, doublons par similarité locale
   (mots du nom, de la description et des tags), fusion confirmée, votes secrets,
   scores, consensus et controverses ; ajout des idées et des classements au
   compte rendu des réunions.
3. **Débat et communauté** : sujets à discuter, départages avec quorum, sondages,
   auto-rôles sécurisés (jamais de rôle à permission dangereuse ni au-dessus du bot),
   ressources partagées (HTTP et HTTPS uniquement, aucune requête vers les URL).
4. **Finitions** : renommage Samae, palette et icônes communes, pagination
   générale, revue de sécurité, documentation complète des commandes.
