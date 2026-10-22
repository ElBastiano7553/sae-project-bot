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
   - Un secret de moins de 16 caractères, un port ou un identifiant de projet
     invalide **désactivent la V2** (message d'erreur au démarrage et dans
     `/health`), mais le reste du bot démarre normalement.

3. Relancez le bot (`Ctrl+C` puis `python -m src.main`). **Résultat attendu** :

   ```
   ... | INFO | src.database.connection | Migration de la base de données vers la version 2
   ... | INFO | src.database.connection | Migration de la base de données vers la version 3
   ... | INFO | src.gitlab.webhook | Webhooks GitLab : écoute sur http://127.0.0.1:8080/webhooks/gitlab
   ```

   (Les lignes « Migration » n'apparaissent qu'une fois.) Dans Discord, `/health`
   affiche **GitLab ✅ webhooks actifs**.

   Si Windows affiche une fenêtre « Pare-feu Windows Defender » pour Python,
   cliquez **Annuler** : avec `127.0.0.1`, aucune connexion extérieure n'est
   nécessaire, le tunnel passe par l'intérieur du PC.

## 4. Test local, sans GitLab

Vous aurez besoin de **trois terminaux** PowerShell dans VS Code (bouton **+**
du panneau Terminal), chacun avec l'environnement virtuel activé
(`.\.venv\Scripts\Activate.ps1`) :

| Terminal | Commande | Reste ouvert ? |
|---|---|---|
| 1 | `python -m src.main` (le bot) | oui |
| 2 | le tunnel (étape 5) | oui |
| 3 | les commandes de test | non |

Dans le **terminal 3**, le bot tournant dans le terminal 1 :

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
pouvez supprimer les messages ensuite. Si `GITLAB_PROJECT_ID` est rempli, le script
utilise automatiquement cet identifiant pour ne pas être refusé (403).

## 5. Ouvrir un tunnel HTTPS

Votre PC n'a pas d'adresse joignable depuis Internet. Le tunnel est un petit
programme qui ouvre une connexion **sortante** vers un service (Cloudflare ou
ngrok) ; ce service vous donne une adresse publique `https://…` et renvoie
chaque requête reçue vers `http://127.0.0.1:8080` sur votre PC. Le chiffrement
HTTPS est assuré par le service ; GitLab exige d'ailleurs un certificat valide
(« SSL verification »).

Choisissez **une** option et lancez-la dans le **terminal 2**. Le tunnel s'arrête
si vous fermez le terminal, éteignez le PC ou s'il se met en veille : les
notifications sont alors perdues côté GitLab (voir « Recent events »).

### Option A : Cloudflare Quick Tunnel (recommandée pour tester, sans compte)

1. Installation (une seule fois) :

   ```powershell
   winget install --id Cloudflare.cloudflared
   ```

   **Fermez puis rouvrez VS Code** (sinon la commande n'est pas trouvée), puis vérifiez :

   ```powershell
   cloudflared --version
   ```

   Résultat attendu : `cloudflared version 202x.x.x ...`. Si `winget` n'existe pas,
   téléchargez `cloudflared-windows-amd64.msi` depuis
   <https://github.com/cloudflare/cloudflared/releases/latest>.

2. Lancement (à chaque session de travail) :

   ```powershell
   cloudflared tunnel --url http://127.0.0.1:8080
   ```

   Au bout de quelques secondes, un cadre s'affiche :

   ```
   Your quick Tunnel has been created! Visit it at (it may take some time to be reachable):
   https://mots-au-hasard-exemple.trycloudflare.com
   ```

   Cette adresse est **votre URL publique**. ⚠️ Elle **change à chaque lancement** :
   il faut alors mettre à jour l'URL du webhook dans GitLab (étape 6). Cloudflare
   ne garantit pas la disponibilité de ces tunnels gratuits : parfait pour tester,
   pas pour un usage permanent.

### Option B : ngrok (compte gratuit, adresse fixe)

1. Créez un compte sur <https://ngrok.com>, puis installez ngrok :

   ```powershell
   winget install --id Ngrok.Ngrok
   ```

   Fermez et rouvrez VS Code, puis enregistrez votre jeton (affiché dans le
   tableau de bord ngrok, rubrique « Your Authtoken ») :

   ```powershell
   ngrok config add-authtoken VOTRE_JETON_NGROK
   ```

   Ce jeton est un **secret** : il est stocké dans votre profil Windows, ne le
   mettez ni dans le dépôt ni dans Discord.

2. Dans le tableau de bord ngrok, rubrique **Domains**, notez votre domaine
   gratuit (par exemple `exemple-mot.ngrok-free.app`), puis lancez :

   ```powershell
   ngrok http 8080 --url https://exemple-mot.ngrok-free.app
   ```

   L'adresse reste la même d'un lancement à l'autre : le webhook GitLab n'est à
   configurer qu'une fois.

### Option C : Cloudflare Tunnel nommé (adresse fixe, nécessite un nom de domaine géré par Cloudflare)

Suivez <https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/>
en faisant pointer un sous-domaine (par exemple `bot.votre-domaine.fr`) vers
`http://127.0.0.1:8080`. Utile si l'équipe veut un bot disponible en permanence.

### Vérifier le tunnel (terminal 3)

Remplacez `VOTRE-ADRESSE` par l'adresse du tunnel, **sans** `/` final :

```powershell
curl.exe -i https://VOTRE-ADRESSE/webhooks/gitlab
```

Résultat attendu : `HTTP/1.1 405 Method Not Allowed` (ou `HTTP/2 405`). C'est
normal et c'est la preuve que la requête traverse le tunnel jusqu'au bot, qui
n'accepte que les envois (POST) de GitLab. Un `502`/`530` ou « Could not resolve
host » signifie que le tunnel ou le bot n'est pas lancé.

Puis le même test qu'à l'étape 4, mais à travers Internet :

```powershell
python -m src.gitlab.demo push --url https://VOTRE-ADRESSE/webhooks/gitlab
python -m src.gitlab.demo push --url https://VOTRE-ADRESSE/webhooks/gitlab --wrong-secret
```

Résultats attendus : `HTTP 200 {"status": "queued"}` avec une notification sur
Discord, puis `HTTP 401` sans notification. Le script refuse une adresse en
`http://` autre que celle du PC, pour ne jamais envoyer le secret en clair.

## 6. Créer le webhook dans GitLab

1. Projet GitLab > **Settings** (Paramètres) > **Webhooks** > **Add new webhook**.
2. **URL** : `https://VOTRE-ADRESSE/webhooks/gitlab` (n'oubliez pas `/webhooks/gitlab`).
3. **Secret token** : collez la valeur de `GITLAB_WEBHOOK_SECRET`.
4. **Trigger** : cochez **Push events** (toutes les branches, le filtrage se fait
   dans `config/gitlab.toml`), **Merge request events** et **Pipeline events**.
   Laissez les autres décochés (ils seraient acceptés mais ignorés).
5. **SSL verification** : laissez **activé**.
6. **Add webhook**.

Avec l'option A, à chaque nouvelle adresse : **Edit** sur le webhook, changez
l'URL, **Save changes** (le secret est conservé).

### Tester depuis GitLab

Sur la ligne du webhook, menu **Test** > **Push events**. GitLab affiche
« Hook executed successfully: HTTP 200 » et une notification apparaît **une seule
fois** dans `#gitlab-activite`. Faites de même avec « Merge request events » et
« Pipeline events » (GitLab exige qu'au moins une MR ou un pipeline existe déjà).

L'historique des appels (code de réponse, corps envoyé et reçu) est visible en
modifiant le webhook, section **Recent events** ; le bouton **View details**
montre la réponse du bot (`queued`, `duplicate`, `ignored`…).

### Tests réels, à cocher

| # | Action | Résultat attendu dans Discord | Réponse dans « Recent events » |
|---|---|---|---|
| 1 | Pousser un commit sur une branche | `#gitlab-activite` : « 📤 1 commit sur … » avec le lien du commit | 200 `queued` |
| 2 | Créer une nouvelle branche sans commit (`git push -u origin nouvelle-branche`) | « 🌱 Nouvelle branche » | 200 `queued` |
| 3 | Ouvrir une MR | « 🆕 MR !n ouverte » avec les branches source → cible | 200 `queued` |
| 4 | Changer le titre ou les labels de la MR | « ✏️ MR !n mise à jour », champ « Modifié » | 200 `queued` |
| 5 | Pousser un commit sur la branche de la MR | une seule notification (le push), pas de « mise à jour » de MR | 200 `queued` puis 200 `ignored` |
| 6 | Approuver puis fusionner la MR | « 👍 approuvée » puis « 🎉 fusionnée » | 200 `queued` |
| 7 | Pipeline réussi (projet avec `.gitlab-ci.yml`) | `#builds-et-tests` : « ✅ Pipeline #n réussi » | 200 `ignored` pour « running », 200 `queued` pour « success » |
| 8 | Pipeline en échec (faites échouer un test exprès) | « ❌ Pipeline #n en échec » avec le job fautif | 200 `queued` |
| 9 | Dans GitLab, **Edit** du webhook : remplacez le secret par `faux`, puis **Test** | rien | **401** — remettez ensuite le bon secret |
| 10 | Arrêter le bot (`Ctrl+C`), pousser un commit, relancer le bot, puis **Resend Request** sur la ligne en erreur de « Recent events » | rien pendant l'arrêt, puis la notification après le renvoi | erreur 502/530, puis 200 `queued` |
| 11 | Commit contenant `@everyone` dans son message | le texte apparaît mais **personne n'est notifié** | 200 `queued` |

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
| `/health` : GitLab ❌ « configuration invalide » | secret de moins de 16 caractères, `WEBHOOK_PORT` ou `GITLAB_PROJECT_ID` invalide | Corrigez `.env` (le message dit quelle variable), relancez. La V1 fonctionne en attendant. |
| `/health` : GitLab ❌ avec une erreur sur `gitlab.toml` | fichier `config/gitlab.toml` mal rempli | Corrigez la ligne indiquée, relancez. |
| `impossible d'écouter sur 127.0.0.1:8080` | port déjà utilisé (autre programme, bot lancé deux fois) | Fermez l'autre programme ou changez `WEBHOOK_PORT` (et le port du tunnel). La V1 continue de fonctionner. |
| GitLab : `HTTP 401` | secret différent dans GitLab et `.env` | Recollez le même secret des deux côtés, sans espace. |
| GitLab : `HTTP 403` | mauvais `GITLAB_PROJECT_ID` | Vérifiez l'identifiant numérique du projet. |
| GitLab : erreur de connexion, `HTTP 502` ou `530` | tunnel arrêté, URL périmée (option A) ou bot arrêté | Relancez le tunnel et le bot, mettez à jour l'URL du webhook. |
| GitLab répond 200 mais rien dans Discord | `/setup` jamais confirmé, salon renommé ou permissions manquantes | Lisez `logs/bot.log`, relancez `/setup`, vérifiez les permissions du bot dans le salon. |
| GitLab affiche le webhook comme « désactivé » ou « temporairement désactivé » | trop d'échecs consécutifs (tunnel ou bot arrêtés longtemps) | Relancez bot et tunnel, puis **Edit** > **Save changes** ou **Test** sur le webhook pour le réactiver. |
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
- L'adresse du tunnel est publique, mais inutile sans le secret. Le serveur
  n'écoute que sur `127.0.0.1` : il n'est joignable que par le tunnel.
- Les réponses d'erreur et les journaux ne recopient jamais les en-têtes reçus,
  et seulement un extrait court (40 caractères) des valeurs invalides.
- Une configuration GitLab invalide ou un port occupé désactivent seulement la V2 :
  la V1 (messages permanents, commandes) continue de fonctionner.
- Le bot n'utilise pas encore de token GitLab : la V2 ne fait que **recevoir**
  des événements. `GITLAB_ACCESS_TOKEN` servira en V3 (lecture seule).

## 10. Limites connues de la V2

- Pendant que le bot ou le tunnel est arrêté, GitLab ne peut rien livrer : il faut
  renvoyer les événements à la main (**Resend Request**), sinon ils sont perdus.
- Une notification acceptée mais pas encore publiée au moment d'un arrêt du bot est
  republiée au redémarrage (jusqu'à 24 h). Si l'arrêt survient pile entre l'envoi
  sur Discord et son enregistrement, elle peut apparaître deux fois.
- Si Discord refuse l'envoi trois fois de suite (salon supprimé, permission
  retirée, panne), la notification est abandonnée et l'erreur est journalisée.
- Les événements Issues, commentaires et tags ne sont pas encore notifiés.
- `config/gitlab.toml` n'est relu qu'au démarrage.
