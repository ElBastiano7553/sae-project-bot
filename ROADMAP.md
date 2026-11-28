# Feuille de route

Rien de ce qui suit n'est commencé : le socle 1.15.0 doit d'abord être fusionné, déployé et testé sur
un serveur de test. Ordre proposé, à valider par l'équipe.

## Étape 0 : mettre le socle en service (Romain, sans développement)

1. Fusionner la PR #26 (1.14.0), puis la branche `claude/socle-1.15` (1.15.0) ; CI verte sur 3.11 à 3.13.
2. `bash deploy/update.sh` sur le VPS (migrations v15 et v16, sauvegardes automatiques).
3. Dérouler `docs/test-manuel.md` (sections 1.14.0 et 1.15.0) sur un serveur de test.
4. Partager le Google Sheet par lien en lecture, remplir `TRACKER_GOOGLE_SHEET` et `GITHUB_SYNC_REPOSITORY`.
5. Trancher la question Q-014 du classeur (5 fonctionnalités VALIDÉ sans décision).

## Priorité 1

### 1. Résumé des modifications du code GitHub par l'IA

- **Objectif** : `/ia bilan` et `/ia relecture` ne couvrent que GitLab, alors que le jeu est sur GitHub.
  Résumer la semaine de code (commits, PR fusionnées) et relire une PR GitHub.
- **Fonctionnement** : réutiliser les éléments enregistrés par la synchronisation (`repo_sync_items`)
  et l'API GitHub en lecture (diff d'une PR) ; même garde-fous que l'IA actuelle (quota, masquage des
  secrets, facultative).
- **Modules** : `src/github/repo_sync.py` (lecture du diff), `src/services/ai_assistant.py`, `cogs/ai.py`.
- **Dépendances** : `AI_PROVIDER=anthropic` et une clé avec plafond ; `GITHUB_SYNC_TOKEN` si le dépôt est privé.
- **Difficultés** : taille des diffs (découpage, limite de jetons), coût, ne jamais envoyer de secret à l'IA.
- **Tests** : faux GitHub (diff), faux fournisseur d'IA (`src/ai/fake.py`), quotas, masquage.
- **Contraintes Discord** : réponse en moins de 3 s (`defer`), 4096 caractères par description d'embed (pages ou fichier joint).

### 2. Tableau d'avancement du projet dans un salon

- **Objectif** : voir l'avancement sans taper de commande.
- **Fonctionnement** : un message permanent (mécanisme existant des messages permanents) mis à jour
  après chaque import, validation ou synchronisation : fonctionnalités par statut, questions ouvertes,
  propositions en attente, résultat de la dernière vérification.
- **Modules** : `src/services/tracker.py`, `src/services/permanent_messages.py`, `config/server.toml` (salon).
- **Difficultés** : éviter les mises à jour en rafale (regroupement, comme le tableau de bord GitLab).
- **Tests** : rendu du message à partir d'un suivi de test ; aucune mise à jour sans changement.
- **Contraintes Discord** : limites d'embed ; le message ne mentionne personne.

### 3. Prévenir les admins des propositions en attente

- **Objectif** : qu'une proposition de la synchronisation ou d'un membre ne reste pas oubliée.
- **Fonctionnement** : un message récapitulatif dans le salon du staff (au plus un par jour), avec
  boutons « Voir » ; la validation reste dans `/suivi valider`.
- **Modules** : `src/services/tracker.py`, boucle de maintenance de `bot.py`, `cogs/tracker.py`.
- **Tests** : regroupement, aucun message sans proposition, boutons persistants (`DynamicItem`).

## Priorité 2

### 4. Déconnecter un projet (étape B6)

- **Objectif** : retirer proprement un projet GitHub ou GitLab relié par erreur.
- **Fonctionnement** : `/projet deconnecter` avec aperçu et confirmation ; le lien et le secret du
  webhook sont oubliés, l'historique reste.
- **Modules** : `src/services/projects.py`, `src/database/projects.py`, `cogs/projects.py`.
- **Difficultés** : aucune suppression de données ; le webhook côté GitHub/GitLab est à retirer par la personne.
- **Tests** : permissions, confirmation, événements refusés après la déconnexion.

### 5. Sondages (`/sondage`)

- **Objectif** : décisions rapides hors réunion (Samae 2.0, lot 3).
- **Fonctionnement** : création par un membre, vote par boutons ou menu, clôture manuelle ou à date,
  résultats anonymes par défaut ; possibilité d'en faire une proposition de décision (validée par un admin).
- **Modules** : nouveaux `services/polls.py`, `database/polls.py` (migration additive), `cogs/polls.py`.
- **Contraintes Discord** : boutons persistants (`DynamicItem`), 25 options au plus par menu.
- **Tests** : un vote par personne, modification, clôture, redémarrage du bot pendant un sondage.

### 6. Ressources partagées (`/ressources`)

- **Objectif** : retrouver les liens utiles (documents, tutoriels, assets).
- **Fonctionnement** : ajout, recherche, modification, retrait ; liens HTTP(S) seulement, **aucune
  requête** vers les adresses.
- **Modules** : nouveaux `services/resources.py`, `database/resources.py`, `cogs/resources.py`.
- **Tests** : validation des adresses, recherche, permissions de modification.

## Priorité 3 (à décider)

### 7. Écrire les validations dans le Google Sheet

- **Objectif** : ne plus recopier à la main les propositions validées.
- **Fonctionnement** : API Google Sheets avec un compte de service limité à ce classeur ; écriture
  seulement après validation d'un admin, ligne ajoutée au CHANGELOG, vérification avant et après.
- **Difficultés** : identifiants Google à créer et protéger ; risque d'abîmer le classeur. La règle
  actuelle (« le classeur n'est jamais modifié ») devra être changée explicitement par l'équipe.

### 8. Parler à Claude Code depuis Discord

- Plugin officiel `discord` ([docs/claude-code-discord.md](docs/claude-code-discord.md)) : Bun, bot
  dédié distinct de Samae, appairage. Ne marche que pendant une session Claude Code ouverte sur le PC.
  Aucun développement dans Samae.
