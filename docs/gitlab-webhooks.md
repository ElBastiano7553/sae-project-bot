# Notifications GitLab (V2) : configuration et tests

La V2 publie dans Discord les événements GitLab utiles à l'équipe :

| Événement GitLab | Ce qui est notifié | Salon par défaut |
|---|---|---|
| Push | commits poussés (5 plus récents), nouvelle branche, branche supprimée | `#gitlab-activite` |
| Merge Request | ouverture, réouverture, modification (titre, description, labels, brouillon, assignés, relecteurs, branche cible), approbation, fusion, fermeture | `#gitlab-activite` |
| Pipeline | statut final : réussi, en échec (avec les jobs en échec), annulé | `#builds-et-tests` |

Les salons, les branches suivies et les actions notifiées se règlent dans
[`config/gitlab.toml`](../config/gitlab.toml) (redémarrez le bot après modification).

> 🔒 **La V2 est désactivée tant que `GITLAB_WEBHOOK_SECRET` est vide.** Sans ce
> secret, aucun serveur web n'est démarré et le bot fonctionne exactement comme en V1.

---

## 1. Comment ça marche

```
GitLab ──HTTPS──► tunnel (Cloudflare ou ngrok) ──► http://127.0.0.1:8080/webhooks/gitlab
                                                        │  serveur web intégré au bot
                                                        ▼
                     1. secret X-Gitlab-Token vérifié (sinon 401, rien n'est lu)
                     2. JSON valide, 2 Mo maximum, bon type d'événement (sinon 400/413/415)
                     3. bon projet si GITLAB_PROJECT_ID est défini (sinon 403)
                     4. événement déjà reçu ? → « duplicate », pas de second message
                                                        │
                                                        ▼
                     réponse immédiate à GitLab, puis message dans le salon Discord
```

Votre PC n'est pas joignable depuis Internet : GitLab ne peut pas l'appeler
directement. Un **tunnel** crée une adresse publique `https://…` qui renvoie les
requêtes vers le bot, sans ouvrir de port sur votre box ni dans le pare-feu Windows.

## 2. Prérequis

- La V1 fonctionne : le bot démarre, `/setup` a été **confirmé** (c'est lui qui
  associe `#gitlab-activite` et `#builds-et-tests`). Sans cela, les notifications
  sont ignorées et un avertissement apparaît dans `logs/bot.log`.
- Le bot peut **voir**, **envoyer des messages** et **intégrer des liens** dans
  `#gitlab-activite` et `#builds-et-tests`.
- Vous êtes **Maintainer** (ou Owner) du projet GitLab : c'est nécessaire pour
  créer un webhook.

## 3. Créer le secret et remplir `.env`

1. Générez un secret aléatoire dans le terminal de VS Code (environnement virtuel activé) :

   ```powershell
   python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```

2. Copiez la valeur affichée dans `.env` (et **nulle part ailleurs** que dans GitLab, étape 5) :

   ```ini
   GITLAB_WEBHOOK_SECRET=la-valeur-generee
   GITLAB_PROJECT_ID=12345678
   WEBHOOK_HOST=127.0.0.1
   WEBHOOK_PORT=8080
   ```

   - `GITLAB_PROJECT_ID` (facultatif mais conseillé) : l'identifiant numérique
     affiché sur la page d'accueil du projet GitLab (« Project ID », bouton ⋮ en
     haut à droite). Les webhooks d'un autre projet sont alors refusés (403).
   - Gardez `WEBHOOK_HOST=127.0.0.1` : seul le tunnel, lancé sur le même PC, doit
     pouvoir joindre le bot.
   - Un secret de moins de 16 caractères est refusé au démarrage.

3. Relancez le bot (`Ctrl+C` puis `python -m src.main`). **Résultat attendu** :

   ```
   ... | INFO | src.database.connection | Migration de la base de données vers la version 2
   ... | INFO | src.gitlab.webhook | Webhooks GitLab : écoute sur http://127.0.0.1:8080/webhooks/gitlab
   ```

   Dans Discord, `/health` affiche **GitLab ✅ webhooks actifs**.

## 4. Test local, sans GitLab

Dans un **second terminal** VS Code (environnement virtuel activé), le bot restant
lancé dans le premier :

```powershell
python -m src.gitlab.demo push
python -m src.gitlab.demo merge_request
python -m src.gitlab.demo pipeline
python -m src.gitlab.demo push --wrong-secret
```

**Résultat attendu** :

- les trois premières commandes affichent `HTTP 200 {"status": "queued"}` et un
  exemple de notification apparaît dans `#gitlab-activite` (push, MR) et
  `#builds-et-tests` (pipeline en échec) ;
- la dernière affiche `HTTP 401 {"status": "unauthorized", ...}` et **rien**
  n'apparaît dans Discord. Le journal du bot indique « secret absent ou incorrect ».

Ces exemples utilisent de fausses données (« epita/sae-game », Alice, Bob) : vous
pouvez supprimer les messages ensuite.

## 5. Ouvrir un tunnel

Choisissez **une** option. Le tunnel doit rester lancé (dans un troisième
terminal) tant que vous voulez recevoir des notifications.

### Option A : Cloudflare Quick Tunnel (le plus simple, sans compte)

```powershell
winget install --id Cloudflare.cloudflared
cloudflared tunnel --url http://127.0.0.1:8080
```

Recopiez l'adresse affichée, de la forme `https://mots-au-hasard.trycloudflare.com`.
⚠️ Elle **change à chaque lancement** : il faudra alors modifier l'URL du webhook
dans GitLab. Idéal pour tester ; pour un usage quotidien, préférez B ou C.

### Option B : ngrok (compte gratuit, adresse fixe)

1. Créez un compte sur <https://ngrok.com>, installez ngrok
   (`winget install --id Ngrok.Ngrok`) et enregistrez votre « authtoken » comme
   indiqué sur leur tableau de bord (c'est un secret de plus : il reste sur votre PC).
2. Le plan gratuit fournit un domaine fixe (« dev domain ») affiché dans le tableau de bord :

   ```powershell
   ngrok http 8080 --url https://votre-domaine.ngrok-free.app
   ```

### Option C : Cloudflare Tunnel nommé (adresse fixe, nécessite un nom de domaine géré par Cloudflare)

Suivez <https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/>
en faisant pointer un sous-domaine (par exemple `bot.votre-domaine.fr`) vers
`http://127.0.0.1:8080`.

### Vérifier le tunnel

```powershell
python -m src.gitlab.demo push --url https://VOTRE-ADRESSE/webhooks/gitlab
```

doit donner le même résultat qu'à l'étape 4.

## 6. Créer le webhook dans GitLab

1. Projet GitLab > **Settings** (Paramètres) > **Webhooks** > **Add new webhook**.
2. **URL** : `https://VOTRE-ADRESSE/webhooks/gitlab` (n'oubliez pas `/webhooks/gitlab`).
3. **Secret token** : collez la valeur de `GITLAB_WEBHOOK_SECRET`.
4. **Trigger** : cochez **Push events** (toutes les branches, le filtrage se fait
   dans `config/gitlab.toml`), **Merge request events** et **Pipeline events**.
   Laissez les autres décochés (ils seraient acceptés mais ignorés).
5. **SSL verification** : laissez **activé**.
6. **Add webhook**.

### Tester depuis GitLab

Sur la ligne du webhook, menu **Test** > **Push events**. GitLab affiche
« Hook executed successfully: HTTP 200 » et une notification apparaît **une seule
fois** dans `#gitlab-activite`. Faites de même avec « Merge request events » et
« Pipeline events » (GitLab exige qu'au moins une MR ou un pipeline existe déjà).

L'historique des appels (code de réponse, corps envoyé et reçu) est visible en
modifiant le webhook, section **Recent events**.

### Test en conditions réelles

- poussez un commit → notification « 📤 1 commit sur … » ;
- ouvrez une MR, changez son titre, approuvez-la, fusionnez-la → une notification
  par étape (les nouveaux commits sur la MR ne sont pas renotifiés : le push suffit) ;
- si le projet a une CI (`.gitlab-ci.yml`), attendez la fin du pipeline →
  « ✅ réussi » ou « ❌ en échec » avec les jobs fautifs dans `#builds-et-tests`.

## 7. Réponses du bot (visibles dans « Recent events »)

| Code | `status` | Signification |
|---|---|---|
| 200 | `queued` | accepté, la notification va être publiée |
| 200 | `duplicate` | déjà reçu (même identifiant de livraison) : pas de second message |
| 200 | `ignored` | authentifié mais non notifié (type non géré, branche non suivie, pipeline en cours…) |
| 400 | `invalid_json` / `invalid_payload` | corps illisible ou incomplet |
| 401 | `unauthorized` | secret absent ou incorrect |
| 403 | `forbidden_project` | projet différent de `GITLAB_PROJECT_ID` |
| 413 | `payload_too_large` | plus de 2 Mo |
| 415 | `unsupported_media_type` | le corps n'est pas du JSON |
| 503 | `busy` | trop de notifications en attente : GitLab réessaiera |

Les événements non gérés reçoivent 200 et non une erreur, car GitLab désactive
automatiquement un webhook qui échoue trop souvent.

## 8. Problèmes fréquents

| Symptôme | Cause probable | Solution |
|---|---|---|
| `/health` : GitLab ⏸️ désactivé | `GITLAB_WEBHOOK_SECRET` vide | Remplissez-le (étape 3) et relancez le bot. |
| `GITLAB_WEBHOOK_SECRET est trop court` | moins de 16 caractères | Générez-en un avec la commande de l'étape 3. |
| `impossible d'écouter sur 127.0.0.1:8080` | port déjà utilisé (autre programme, bot lancé deux fois) | Fermez l'autre programme ou changez `WEBHOOK_PORT` (et le port du tunnel). La V1 continue de fonctionner. |
| GitLab : `HTTP 401` | secret différent dans GitLab et `.env` | Recollez le même secret des deux côtés, sans espace. |
| GitLab : `HTTP 403` | mauvais `GITLAB_PROJECT_ID` | Vérifiez l'identifiant numérique du projet. |
| GitLab : erreur de connexion, `HTTP 502` ou `530` | tunnel arrêté, URL périmée (option A) ou bot arrêté | Relancez le tunnel et le bot, mettez à jour l'URL du webhook. |
| GitLab répond 200 mais rien dans Discord | `/setup` jamais confirmé, salon renommé ou permissions manquantes | Lisez `logs/bot.log`, relancez `/setup`, vérifiez les permissions du bot dans le salon. |
| `Invalid url given` dans GitLab | GitLab refuse certaines adresses (réseau local) | Utilisez l'adresse `https://` du tunnel, jamais `127.0.0.1`. |
| GitLab de l'école (instance interne) n'arrive pas à joindre le tunnel | l'instance peut bloquer les appels vers Internet | Testez avec le bouton **Test** ; si l'échec persiste, demandez aux administrateurs ou passez par un hébergement (V5). |

## 9. Sécurité

- Le secret n'est jamais affiché (ni dans les journaux, ni dans `/config`, ni dans
  les réponses HTTP). En cas de fuite : générez-en un nouveau, mettez-le dans
  `.env` et dans GitLab, relancez le bot.
- Le secret est vérifié **avant** de lire le corps de la requête, avec une
  comparaison à temps constant.
- Les textes venant de GitLab (messages de commit, titres de MR…) sont nettoyés :
  pas de mention `@everyone`, pas de faux lien, Markdown neutralisé, longueur bornée.
  Les messages sont envoyés avec toutes les mentions désactivées.
- L'adresse du tunnel est publique, mais inutile sans le secret.
- Le bot n'utilise pas encore de token GitLab : la V2 ne fait que **recevoir**
  des événements. `GITLAB_ACCESS_TOKEN` servira en V3 (lecture seule).

## 10. Limites connues de la V2

- Les notifications en attente au moment d'un arrêt brutal du bot sont perdues
  (GitLab ne les renvoie pas, car il a déjà reçu sa réponse 200).
- Les événements Issues, commentaires et tags ne sont pas encore notifiés.
- `config/gitlab.toml` n'est relu qu'au démarrage.
