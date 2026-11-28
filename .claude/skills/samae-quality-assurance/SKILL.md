---
name: samae-quality-assurance
description: Processus de validation de Samae avant tout commit ou pull request : tests pytest (faux Discord, faux GitHub/GitLab/Google), tests de régression, lint ruff, permissions, sécurité, gestion des erreurs, documentation. À suivre pour chaque modification importante du dépôt sae-project-bot.
---

# Samae : assurance qualité

Une modification n'est terminée que lorsque toutes les étapes ci-dessous sont faites, ou que celle qui
ne l'est pas est **dite** dans le compte rendu (jamais « testé » pour un test qui n'a pas eu lieu).

## 1. Avant de modifier

- Lancer `python -m pytest -q` pour connaître l'état de départ (nombre de tests réussis).
- Lire le skill `samae-project-context` et le code concerné en entier.

## 2. Tests à écrire

| Changement | Test attendu |
|---|---|
| Service métier | Test unitaire sans Discord (fixture `db` de `tests/conftest.py`) : cas nominal, cas limites, refus de permission (`Actor` non admin, autre serveur) |
| Commande Discord | Faux Discord (`FakeInteraction`, `FakeUser`, `FakeChannel` de `tests/test_meeting_commands.py`) : réponse éphémère, texte attendu, refus pour un non-admin |
| Appel réseau (GitHub, GitLab, Google) | Faux serveur local `aiohttp.test_utils.TestServer` : réponse normale, 401/403, 404, 429 ou limite de débit, 5xx, réponse inattendue |
| Migration | `tests/test_database.py` (version = `len(MIGRATIONS)`), migration « création seulement » vérifiée par un test (pas de DROP, DELETE, UPDATE, ALTER sur des données) |
| Configuration | `Settings.from_env` : valeur valide, valeur invalide ⇒ `ConfigError` au message clair, secret absent de `repr` |
| Correction de bug | Test qui échoue avant la correction (régression) |
| Asynchrone | Pas de `sleep` réel long ; `retry_delays=()` ou `(0,)` dans les tests ; arrêt propre des tâches |

Aucun test ne doit utiliser le réseau, un vrai token, ni le vrai contenu du classeur.

## 3. Vérifications automatiques

```powershell
python -m ruff check src tests
python -m pytest -q
python -c "import src.main, src.discord_bot.bot"
```

La CI (`.github/workflows/tests.yml`) relance ruff et pytest sur Python 3.11, 3.12 et 3.13 et vérifie
l'image Docker (sans root, sans `.env`, arrêt code 2 sans configuration).

## 4. Revue

- Sécurité : aucun secret écrit en clair (journaux, Discord, Git) ; entrées utilisateur nettoyées ;
  adresses externes limitées aux hôtes prévus ; aucun `eval`, `pickle`, `shell=True` ; dépendance
  ajoutée = version épinglée, licence vérifiée, `pip-audit -r requirements.txt` propre.
- Permissions : action d'admin = `@admin_only()` + vérification dans le service + test de refus.
- Robustesse : une intégration en panne ne fait pas tomber le bot ; erreurs réseau traduites ; tâche de
  fond protégée (`try/except Exception` + `log.exception`) ; `defer` avant un travail lent.
- Outils de Claude Code disponibles : `/code-review` (bugs), `/security-review` (sécurité),
  `/simplify` (lisibilité) sur la branche avant la PR.

## 5. Documentation et compte rendu

- Mettre à jour `CHANGELOG.md`, `docs/commands.md`, `.env.example`, `docs/test-manuel.md` (ce qui reste
  à tester sur un vrai serveur), le guide du domaine.
- Le compte rendu distingue : testé automatiquement (nombre de tests, version de Python), testé à la
  main, **non testé sur Discord / en production**.
