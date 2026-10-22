# Notifications GitHub du jeu : configuration et tests

Samae peut publier dans `#gitlab-activite` l'activité du dépôt GitHub **privé**
`ElBastiano7553/game-project`. Cette intégration est **indépendante** des webhooks
GitLab : son propre secret, son propre port, sa propre table d'historique. Les
notifications GitLab continuent de fonctionner exactement comme avant.

| Événement GitHub (`X-GitHub-Event`) | Ce qui est notifié | Ce qui est ignoré (pour éviter le bruit) |
|---|---|---|
| `push` | commits poussés (5 plus récents, fichiers, lien de comparaison), nouvelle branche, branche supprimée, push forcé signalé | tags ; commits déjà connus du dépôt (`distinct: false`, avance rapide) |
| `pull_request` | ouverture, réouverture, « prête pour relecture », fusion, fermeture | nouveaux commits (`synchronize`, déjà annoncés par le push), labels, relecteurs, modifications… |
| `issues` | ouverture, fermeture (terminée ou « non prévue »), réouverture | modifications, labels, assignations, transferts… |
| `workflow_run` (GitHub Actions) | exécution **terminée en échec** : `failure`, `timed_out`, `startup_failure` | réussites, annulations, exécutions en cours |
| `ping` | rien (réponse `pong`, envoyée par GitHub à la création du webhook) | — |

Tout autre événement authentifié reçoit `200 ignored` (GitHub n'aime pas les erreurs répétées).

> 🔒 **GitHub est désactivé tant que `GITHUB_WEBHOOK_SECRET` est vide.** Aucun serveur
> supplémentaire n'est alors démarré. Une erreur dans les variables `GITHUB_*` désactive
> **uniquement** GitHub : le reste du bot (GitLab compris) démarre normalement.

---

## 1. Comment ça marche

```
GitHub ──HTTPS──► reverse proxy ou tunnel ──► http://127.0.0.1:8081/webhooks/github
                                                   │  serveur web intégré au bot (port séparé de GitLab)
                                                   ▼
             1. POST uniquement, en-tête X-Hub-Signature-256 présent (sinon 401, corps non lu)
             2. 2 Mo maximum (sinon 413)
             3. signature HMAC-SHA256 du corps vérifiée en temps constant (sinon 401)
             4. Content-Type JSON, JSON objet, X-GitHub-Delivery et X-GitHub-Event valides (sinon 415/400)
             5. dépôt = exactement GITHUB_WEBHOOK_REPOSITORY (sinon 403)
             6. livraison déjà reçue (même X-GitHub-Delivery, ou même fait GitHub) → « duplicate »
                                                   │
                                                   ▼
             réponse immédiate à GitHub, puis message dans #gitlab-activite
             (file d'attente, 3 essais d'envoi, renvoi après redémarrage si < 24 h)
```

Sécurité des messages : les textes venant de GitHub (commits, titres, descriptions,
noms de branches) sont nettoyés comme pour GitLab : aucune mention `@everyone`, `@here`,
`<@…>` ou `<@&…>` ne peut fonctionner, le Markdown est neutralisé, les liens non `https`
sont retirés et les messages sont envoyés avec `allowed_mentions = none`.

Historique : les livraisons GitHub sont enregistrées dans la table `github_deliveries`
(migration v8), **séparée** de `gitlab_events`. `/events`, `/stats`, `/leaderboard`,
`/project` et le tableau de bord restent donc 100 % GitLab. Les livraisons sont oubliées
après `HISTORY_RETENTION_DAYS` jours, comme le reste de l'historique.

## 2. Variables d'environnement (`.env`)

| Variable | Défaut | Rôle |
|---|---|---|
| `GITHUB_WEBHOOK_SECRET` | *(vide)* | Secret partagé avec GitHub, **16 caractères minimum**. Vide = GitHub désactivé. Masqué dans tous les journaux, jamais affiché sur Discord. |
| `GITHUB_WEBHOOK_REPOSITORY` | `ElBastiano7553/game-project` | Seul dépôt accepté (comparaison exacte, majuscules comprises). |
| `GITHUB_WEBHOOK_HOST` | `127.0.0.1` | Adresse d'écoute. Gardez `127.0.0.1` derrière un reverse proxy ou un tunnel sur la même machine (`0.0.0.0` uniquement dans Docker, avec un port publié sur `127.0.0.1`). |
| `GITHUB_WEBHOOK_PORT` | `8081` | Port d'écoute. Doit être différent de `WEBHOOK_PORT` (GitLab, 8080) si GitLab est actif. |

Le nom `GITHUB_REPOSITORY` n'est volontairement **pas** utilisé : GitHub Actions le
définit automatiquement (dépôt du bot) et il ne doit pas changer le dépôt autorisé.

Générer un secret :

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Les notifications vont dans le salon de clé `gitlab_activite` (`config/server.toml`),
associé par `/setup`. S'il n'est pas associé, la livraison est marquée `skipped` et un
avertissement apparaît dans `logs/bot.log`.

## 3. Vérifier que c'est actif

Au démarrage, `logs/bot.log` doit contenir :

```
... | INFO | src.database.connection | Migration de la base de données vers la version 8
... | INFO | src.github.webhook | Webhooks GitHub : écoute sur http://127.0.0.1:8081/webhooks/github
```

Sur Discord :

- `/status` affiche une ligne **GitHub** : ⏸️ désactivé, ❌ erreur (secret trop court,
  port occupé…), ou ✅ actif avec la date du dernier événement. « Accès depuis Internet
  non vérifié » : le bot sait seulement qu'il écoute localement.
- `/config` (admins) affiche le dépôt autorisé et le port, jamais le secret.

## 4. Tester en local, sans GitHub

Bot lancé dans un terminal (`python -m src.main`), dans un second terminal :

```powershell
python -m src.github.demo ping            # → HTTP 200 {"status": "pong"}
python -m src.github.demo push            # → HTTP 200 {"status": "queued"} puis message Discord
python -m src.github.demo pull_request
python -m src.github.demo issues
python -m src.github.demo workflow_run
python -m src.github.demo push --wrong-secret   # → HTTP 401 (doit être refusé)
```

Le script signe un exemple de JSON avec le secret de `.env` ; il n'envoie jamais le
secret lui-même et vise toujours ce PC. Relancer exactement le même exemple répond
`duplicate` (même fait GitHub) : c'est la déduplication.

## 5. Créer le webhook dans GitHub (plus tard, au déploiement)

Ne le faites qu'une fois le bot joignable en HTTPS (voir
[github-deploiement.md](github-deploiement.md)). Le dépôt reste **privé** : les
webhooks fonctionnent sans le rendre public.

1. `https://github.com/ElBastiano7553/game-project` → **Settings** → **Webhooks** →
   **Add webhook** (il faut être administrateur du dépôt).
2. **Payload URL** : `https://<votre-domaine>/webhooks/github`.
3. **Content type** : `application/json` (obligatoire : l'autre format est refusé, 415).
4. **Secret** : la même valeur que `GITHUB_WEBHOOK_SECRET`.
5. **SSL verification** : *Enable*.
6. **Which events** : *Let me select individual events* → cochez **Pushes**,
   **Pull requests**, **Issues** et **Workflow runs** (décochez le reste).
7. **Active** coché → **Add webhook**. GitHub envoie aussitôt un `ping` : dans
   **Recent Deliveries**, la réponse doit être `200` avec `{"status": "pong"}`.

En cas de problème, **Recent Deliveries** montre le code de réponse de chaque livraison :

| Code | Cause probable |
|---|---|
| 401 | secret différent entre GitHub et `.env`, ou signature absente |
| 403 | webhook créé sur un autre dépôt que `GITHUB_WEBHOOK_REPOSITORY` |
| 404 / 405 | mauvaise URL (le chemin doit être exactement `/webhooks/github`) |
| 415 | Content type réglé sur `application/x-www-form-urlencoded` |
| 503 | trop de notifications en attente : GitHub peut la renvoyer (**Redeliver**) |
| délai dépassé | bot arrêté, proxy/tunnel mal configuré, ou port différent de `GITHUB_WEBHOOK_PORT` |

Le bouton **Redeliver** renvoie la même livraison (même `X-GitHub-Delivery`) : si elle
avait déjà été traitée, le bot répond `duplicate` sans republier.

## 6. Limites connues

- La fusion d'une PR produit deux faits GitHub distincts : la PR fusionnée **et** le push
  du commit de fusion sur `main`. Les deux sont annoncés (le second liste le commit de fusion).
- Seuls les échecs de workflows sont annoncés : un retour au vert n'est pas signalé.
- Pas de filtre par branche ni de choix des actions dans un fichier de configuration
  (les règles sont fixées dans `src/github/events.py`).
- Comme pour GitLab, `/status` ne peut pas vérifier que GitHub joint réellement le bot :
  seul l'onglet **Recent Deliveries** de GitHub le prouve.
