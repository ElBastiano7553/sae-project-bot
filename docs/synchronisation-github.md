# Synchronisation du suivi avec le dépôt GitHub (1.15.0)

Les webhooks GitHub annoncent les commits et les pull requests en direct, mais rien ne relie ce code
aux lignes du classeur de suivi, et GitHub ne renvoie jamais un webhook perdu (bot arrêté, VPS
injoignable). La synchronisation relit le dépôt et fait ce lien.

## Fonctionnement

`/suivi synchroniser` (administrateurs), ou automatiquement toutes les `GITHUB_SYNC_INTERVAL_MINUTES` :

1. lecture du dépôt par l'API REST de GitHub, **en lecture seule** : branche principale, commits de
   cette branche depuis la dernière synchronisation (30 jours la première fois, 100 au plus), et les
   50 pull requests modifiées le plus récemment ;
2. chaque commit et chaque PR est enregistré (table `repo_sync_items`, migration v16) avec les
   identifiants du suivi qu'il cite : `FEAT-002` dans un message de commit, le titre ou la
   description d'une PR, ou le nom de sa branche (`feat-002-inventaire`) ;
3. pour les lignes de FEATURES, GAMEPLAY, UI_UX et ART_DIRECTION citées, Samae **propose** un statut :
   - du code arrive sur une ligne **VALIDÉ** → proposition **EN COURS** ;
   - un commit de la branche principale ou une PR **fusionnée** annonce la fin (« termine FEAT-002 »,
     « Closes FEAT-002 », « fix GP-003 ») d'une ligne VALIDÉ ou EN COURS → proposition **TERMINÉ** ;
4. les propositions attendent un administrateur : `/suivi en-attente`, puis `/suivi valider` ou
   `/suivi refuser`. Rien n'est appliqué automatiquement.

Garde-fous :

- une ligne non validée par l'équipe (PROPOSÉ, À DISCUTER, À CLARIFIER) n'est jamais proposée : elle
  est seulement signalée (« code sur des éléments non validés ») ;
- une ligne TERMINÉ ou ABANDONNÉ n'est plus jamais proposée ;
- un même commit n'est lu qu'une fois ; une proposition identique déjà en attente n'est pas refaite ;
- un identifiant cité mais absent du suivi est signalé ;
- la synchronisation automatique n'écrit rien sur Discord (pas de doublon avec les webhooks).

`/suivi code cle:FEAT-002` liste les commits et PR qui citent une ligne.

## Configuration (.env)

| Variable | Rôle |
|---|---|
| `GITHUB_SYNC_REPOSITORY` | Dépôt du jeu, `propriétaire/dépôt`. Vide : celui des webhooks GitHub (`GITHUB_WEBHOOK_REPOSITORY`) s'ils sont configurés, sinon désactivé. |
| `GITHUB_SYNC_TOKEN` | Facultatif pour un dépôt public (60 requêtes par heure sans jeton, une synchronisation en utilise 3). Pour un dépôt privé : jeton *fine-grained* limité à ce dépôt, droits **Contents: Read-only** et **Pull requests: Read-only**. Masqué dans les journaux. |
| `GITHUB_SYNC_INTERVAL_MINUTES` | 0 (défaut) : seulement à la demande. Sinon entre 15 et 10080. |

## Convention pour l'équipe

Citer l'identifiant de la ligne du suivi dans les messages de commit ou les titres de PR :

```
FEAT-004 : première version de l'arbre de compétences
Termine FEAT-002 : inventaire jouable
```

## Fichiers

`src/github/repo_sync.py` (lecture de GitHub), `src/services/repo_sync.py` (lien avec le suivi),
`src/database/repo_sync.py` (table), commandes dans `src/discord_bot/cogs/tracker.py`, tests dans
`tests/test_repo_sync.py`.
