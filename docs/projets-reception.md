# Réception des événements des projets (étape B3, version 1.8.0)

Cette étape ajoute à Samae une **réception commune** des événements GitHub et GitLab pour les
projets enregistrés avec `/projet ajouter`. Elle est **désactivée par défaut** et n'écoute que
sur l'interface locale du serveur (`127.0.0.1`) : rien n'est visible depuis Internet tant que le
propriétaire du VPS n'a pas, de lui-même, ajouté le site Nginx décrit plus bas.

**Ce que B3 ne fait pas encore :** relier un projet à GitHub ou GitLab. Ce lien (installation de la
GitHub App en B4, webhook GitLab à secret unique en B5) est la seule façon pour un projet de
recevoir quelque chose. Tant qu'il n'existe pas, `/projet liste` continue d'afficher « enregistré,
pas encore connecté : aucune notification », et c'est exact.

## 1. Ce qui est reçu, et comment

| Chemin (POST seulement) | Qui l'appelle | Vérifications, dans l'ordre |
|---|---|---|
| `/github/webhook` | la GitHub App (B4) | signature HMAC-SHA256 avec `GITHUB_APP_WEBHOOK_SECRET` (sinon 401, sans lire le corps si elle manque) ; corps ≤ 2 Mo (413) ; JSON (415, 400) ; en-têtes de livraison valides (400) ; le projet est retrouvé par **l'installation de l'App et le dépôt** indiqués dans l'événement : un dépôt relié à aucun projet est ignoré (200) |
| `/gitlab/webhook/<identifiant>` | un projet GitLab (B5) | secret `X-Gitlab-Token` dont l'**empreinte** correspond à celle enregistrée pour cet identifiant (sinon 401, réponse identique pour un identifiant inconnu, sans lire le corps) ; JSON ≤ 2 Mo ; l'événement doit concerner **le projet relié** à ce webhook (403 sinon) |

Tout autre chemin répond 404, une autre méthode 405. Les anciens chemins (`/webhooks/github` sur
8081, `/webhooks/gitlab` sur 8080) n'existent pas sur ce serveur : ils restent servis, sans
changement, par les webhooks historiques du `.env`.

Ensuite, pour **chaque** projet visé :

1. la catégorie de l'événement (code, revues, tickets, CI, déploiements) doit avoir un salon pour
   ce projet (`/projet modifier`), sinon rien ;
2. les filtres de cette catégorie (branches, actions, statuts) sont appliqués ; un événement écarté
   est noté « filtré », sans message ;
3. anti-doublons **par projet** : la même livraison, ou le même fait renvoyé sous un autre
   identifiant, n'est publié qu'une fois ;
4. juste avant l'envoi, Samae revérifie que le salon existe et appartient **au serveur Discord du
   projet** ; sinon rien n'est envoyé (« ignoré »).

Les notifications ont la même mise en forme que celles des webhooks historiques. Une notification
acceptée mais pas encore envoyée est renvoyée après un redémarrage (jusqu'à 24 h).

### Traduction vers le vocabulaire commun

| Catégorie | GitHub | GitLab |
|---|---|---|
| Code | push sur une branche | push sur une branche |
| Revues | PR ouverte → `ouverture`, rouverte → `reouverture`, prête pour relecture → `modification`, fusionnée → `fusion`, fermée → `fermeture` | MR `ouverture`, `reouverture`, `modification`, `fusion`, `fermeture`, `approbation` |
| Tickets | issue `ouverture`, `fermeture`, `reouverture` | (pas encore) |
| CI | workflow en échec → `echoue` (les réussites GitHub ne sont pas notifiées, comme aujourd'hui) | pipeline `reussi`, `echoue`, `annule` |
| Déploiements | (pas encore) | `reussi`, `echoue`, `annule`, `en_cours` |
| Versions | (pas encore) | (pas encore) |

### Isolation entre serveurs Discord

* Un événement GitHub ne va qu'aux projets reliés à **cette** installation de l'App **et** à ce
  dépôt. Un autre serveur qui a seulement *ajouté* le même dépôt ne reçoit rien.
* Un webhook GitLab est propre à **un** projet ; son secret ne fonctionne pour aucun autre.
* Deux serveurs qui suivent et ont relié le même dépôt reçoivent chacun leur notification, dans
  leurs propres salons, avec leurs propres filtres et leur propre anti-doublons.
* Les salons et filtres sont toujours lus avec le serveur du projet, et le salon est revérifié à
  l'envoi.

### Secrets

* `GITHUB_APP_WEBHOOK_SECRET` reste dans `.env` sur le serveur (jamais dans Discord), est masqué
  dans les journaux, et doit être différent de `GITHUB_WEBHOOK_SECRET`.
* Pour GitLab, la base ne contient que l'**empreinte SHA-256** du secret de chaque webhook ; le
  secret lui-même sera affiché une seule fois dans le navigateur à l'étape B5.
* Aucune réponse HTTP ni aucun journal ne contient de secret ou d'empreinte (test dédié).

## 2. Réglages (`.env`)

| Variable | Défaut | Rôle |
|---|---|---|
| `PROJECTS_HTTP_ENABLED` | `false` | `true` pour démarrer la réception |
| `PROJECTS_HTTP_HOST` | `127.0.0.1` | adresse d'écoute ; **seules** `127.0.0.1`, `::1` et `localhost` sont acceptées |
| `PROJECTS_HTTP_PORT` | `8090` | doit différer de `WEBHOOK_PORT` (8080) et `GITHUB_WEBHOOK_PORT` (8081) |
| `GITHUB_APP_WEBHOOK_SECRET` | vide | secret du webhook de la GitHub App (B4) ; vide = `/github/webhook` répond 404 |

Une erreur dans ces variables désactive seulement la réception (affichée dans `/etat`), jamais le
reste du bot ni les webhooks historiques.

`/etat` affiche une ligne « Réception des projets » : désactivée, en erreur, ou « écoute locale
sur http://127.0.0.1:8090 », sans jamais compter les projets (un projet peut être masqué à un membre).

## 3. Base de données (migration v10)

| Table | Contenu |
|---|---|
| `provider_links` | lien d'un projet avec son fournisseur : numéro d'installation de la GitHub App, ou identifiant public du webhook GitLab + empreinte de son secret. Aucun secret en clair |
| `project_deliveries` | une ligne par projet et par livraison : statut (`queued`, `sent`, `failed`, `skipped`, `filtered`), empreinte anti-doublon, salon, notification (JSON) ; conservée `HISTORY_RETENTION_DAYS` jours |

La migration ne fait que créer ces deux tables. Sauvegarde automatique avant migration
(`…-avantv10.sqlite3`) ; retour arrière : restaurer cette sauvegarde avec l'ancienne version.

## 4. Mise en ligne sur le VPS (à faire avant B4, avec l'accord du propriétaire du VPS)

**Rien de ceci n'est nécessaire pour B3**, qui se teste localement. Ces étapes servent à préparer
B4 et B5. Elles ne modifient aucun site existant. Je n'ai pas accès au VPS : le propriétaire lance
les commandes et ne me transmet jamais de secret.

1. **Vérifications en lecture seule** (ne modifient rien) :

   ```bash
   ls -l /etc/nginx/sites-enabled/
   sudo nginx -T 2>/dev/null | grep -n "server_name"
   sudo certbot certificates
   sudo ss -ltnp | grep -E ':(80|443|8080|8081|8090) '
   dig +short samae.exemple.fr
   ```

   Elles disent quels sites existent, si le port 8090 est libre et si le sous-domaine est déjà pris.

2. **DNS** : un enregistrement `A` (et `AAAA` si le VPS a une IPv6) `samae.exemple.fr` vers
   l'adresse du VPS, chez le registraire.

3. **Nginx, partie HTTP seulement** : copier [`deploy/nginx/samae.conf.example`](../deploy/nginx/samae.conf.example)
   dans `/etc/nginx/sites-available/samae`, remplacer le nom de domaine, **mettre en commentaire le
   second bloc `server` (HTTPS)**, puis :

   ```bash
   sudo mkdir -p /var/www/samae-acme
   sudo ln -s /etc/nginx/sites-available/samae /etc/nginx/sites-enabled/samae
   sudo nginx -t && sudo systemctl reload nginx
   ```

   `nginx -t` vérifie la configuration sans rien appliquer ; `reload` recharge sans couper les autres sites.

4. **Certificat** (sans toucher aux autres fichiers Nginx) :

   ```bash
   sudo certbot certonly --webroot -w /var/www/samae-acme -d samae.exemple.fr
   ```

   Le renouvellement automatique déjà en place le prendra en charge.

5. **Nginx, partie HTTPS** : décommenter le second bloc, puis `sudo nginx -t && sudo systemctl reload nginx`.
   Seuls `POST /github/webhook` et `POST /gitlab/webhook/<identifiant>` passent ; tout le reste répond 404.

6. **Samae** : dans `.env`, `PROJECTS_HTTP_ENABLED=true` (et `GITHUB_APP_WEBHOOK_SECRET` en B4),
   puis redémarrage. Contrôle local : `curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8090/gitlab/webhook/essai-0000000000000`
   doit afficher `401`.

**Retour arrière** : `sudo rm /etc/nginx/sites-enabled/samae && sudo nginx -t && sudo systemctl reload nginx`,
puis `PROJECTS_HTTP_ENABLED=false` et redémarrage. Les autres sites ne sont jamais modifiés.

## 5. Tests

`tests/test_projects_reception.py` : migration v9 → v10, liens et isolation, traduction et filtres,
requêtes HTTP réelles (accès autorisés et refusés, signatures, secrets, doublons, filtres, salon
d'un autre serveur, salon supprimé, chemins inexistants), renvoi après redémarrage, réglages,
compatibilité avec les webhooks du `.env`, ligne de `/etat`, et projet connecté masqué à un
membre qui ne voit aucun de ses salons.
