# Connexion des projets GitLab par une application OAuth (étape B5, version 1.10.0)

`/projet configurer projet:<référence>` relie un projet GitLab déjà enregistré (avec `/projet ajouter`)
à Samae **sans aucun token dans Discord**. L'administrateur se connecte à GitLab, GitLab confirme qu'il
est **Maintainer ou Owner** du projet, puis une page affiche **une seule fois** l'adresse et le secret
du webhook à coller dans GitLab. Les événements du projet arrivent ensuite par la réception commune
(étape B3, [projets-reception.md](projets-reception.md)) et sont publiés dans les salons du projet.

**Rien n'est activé par défaut.** Tant que les variables ci-dessous sont vides, `/projet configurer
projet:` répond que la connexion n'est pas activée, et tout le reste de Samae fonctionne comme en 1.9.0.
Le webhook GitLab historique du `.env` (`GITLAB_WEBHOOK_SECRET`, port 8080) ne change pas.

## 1. Le parcours

1. Le projet est enregistré : `/projet ajouter url:https://gitlab.com/groupe/projet`, puis ses salons
   avec `/projet modifier`.
2. Un administrateur lance `/projet configurer projet:<référence>` (l'autocomplétion ne propose que les
   projets GitLab de ce serveur, et seulement aux administrateurs). Samae répond, visible par lui seul,
   avec un bouton **Se connecter avec GitLab**. Le lien porte un jeton personnel : signé, valable
   15 minutes, utilisable une seule fois, lié à ce serveur Discord, à cette personne et **à ce projet**.
3. GitLab demande d'autoriser Samae (lecture seule : `read_user`, `read_api`), puis renvoie la personne
   sur `https://<adresse de Samae>/gitlab/callback`.
4. Samae vérifie le jeton, échange le code contre un accès **temporaire**, lit sur GitLab le rôle de la
   personne sur ce projet, puis **révoque aussitôt** cet accès, que la vérification réussisse ou non.
   - Rôle exigé : **Maintainer (40) ou Owner (50)**. Developer, Reporter, Guest : refusé, rien n'est relié.
   - Le rôle hérité d'un groupe parent compte (Samae lit `members/all` si le rôle direct ne suffit pas).
   - Le projet lu doit être **exactement** celui enregistré (numéro, ou chemin sans tenir compte des
     majuscules). Un projet renommé est refusé plutôt que deviné : enregistrez-le à nouveau.
5. Samae crée un identifiant public de webhook et un secret aléatoire, **n'enregistre que l'empreinte
   SHA-256 du secret**, puis affiche sur la page :
   - l'URL : `https://<adresse de Samae>/gitlab/webhook/<identifiant>` ;
   - le jeton secret, **une seule fois**. Recharger la page affiche « Lien expiré ».
6. Dans GitLab : projet → **Paramètres → Webhooks → Ajouter un webhook**. Coller l'URL et le jeton
   secret ; déclencheurs **Push**, **Merge request**, **Pipeline**, **Deployment** ; vérification SSL
   activée. Puis **Tester → Événements de poussée** : la notification arrive dans le salon « Code ».
7. `/projet configurer projet:<référence>` montre ensuite un bouton **Envoyer un message de test** (si
   le projet a des salons), qui vérifie la publication dans chaque salon de ce serveur.

Relancer la connexion d'un projet déjà relié crée un **nouveau secret** : l'ancien cesse aussitôt de
fonctionner (Samae le dit dans Discord et sur la page) ; il faut alors **modifier** le webhook existant
dans GitLab, pas en ajouter un second.

### Ce que Samae ne fait pas

- Il ne garde **aucun token GitLab** et n'a **aucun droit d'écriture** : il ne crée pas le webhook
  lui-même, c'est la personne qui le colle.
- Le secret du webhook n'est **jamais** dans la base (seulement son empreinte), ni dans les journaux,
  ni dans Discord. S'il est perdu, relancez la connexion pour en créer un nouveau.
- Les jetons des liens ne sont gardés que sous forme d'empreinte SHA-256, sont supprimés dès leur
  utilisation (réussie ou non) et avec le projet s'il est supprimé. Un jeton du parcours GitHub ne vaut
  rien ici (clé de signature différente).
- En cas d'échec (lien invalide, autorisation annulée, code refusé, rôle insuffisant, projet invisible,
  GitLab en panne ou limite d'appels atteinte), **rien n'est enregistré**.
- Les pages ne sont pas mises en cache (`Cache-Control: no-store`), n'envoient pas leur adresse ailleurs
  (`Referrer-Policy: no-referrer`) et interdisent tout script (`Content-Security-Policy`).
- Un projet suivi par le webhook historique (`GITLAB_WEBHOOK_SECRET` avec `GITLAB_PROJECT_ID`, projet
  `env-gitlab`) est refusé, dans Discord ou au retour de GitLab : sinon chaque événement serait publié
  deux fois. Si `GITLAB_PROJECT_ID` est vide, Samae ne peut pas savoir quel projet le webhook historique
  suit : videz `GITLAB_WEBHOOK_SECRET` avant de relier ce projet.

## 2. Prérequis (avec l'accord du propriétaire du VPS)

| Prérequis | Pourquoi | Où |
| --- | --- | --- |
| Sous-domaine HTTPS avec certificat (le même que pour GitHub) | GitLab ne renvoie la personne et n'envoie ses webhooks que vers une adresse joignable | [projets-reception.md](projets-reception.md) |
| Site Nginx `deploy/nginx/samae.conf.example` | laisse passer `POST /gitlab/webhook/<identifiant>` et `GET /gitlab/callback`, rien d'autre ; journal sans paramètres | même guide |
| Réception des projets | `PROJECTS_HTTP_ENABLED=true` | `.env` |
| Application OAuth GitLab | identifie Samae auprès de GitLab | section 3 |

**Rien de tout cela n'a été fait sur le VPS.** Le fichier Nginx n'est qu'un exemple.

## 3. Créer l'application OAuth GitLab (test)

Sur l'instance du projet (gitlab.com ou l'instance de l'école) : **Préférences de l'utilisateur →
Applications → Ajouter une application** (ou au niveau d'un groupe : Paramètres → Applications).

| Champ | Valeur |
| --- | --- |
| Nom | `Samae (test)` |
| Redirect URI | `https://<adresse de Samae>/gitlab/callback` |
| Confidentielle | **oui** |
| Scopes | **`read_user`** et **`read_api`** uniquement |

GitLab affiche l'**Application ID** et le **Secret**. Dans le `.env` du serveur :

```dotenv
PROJECTS_HTTP_ENABLED=true
PROJECTS_PUBLIC_URL=https://samae.exemple.fr
GITLAB_APP_URL=https://gitlab.com          # vide = https://gitlab.com
GITLAB_APP_CLIENT_ID=<Application ID>
GITLAB_APP_CLIENT_SECRET=<Secret>          # masqué dans les journaux, jamais dans Discord
```

Une variable invalide ne désactive **que** la connexion GitLab : `/etat` affiche « connexion GitLab
refusée (raison) », et tout le reste démarre. Un seul `GITLAB_APP_URL` par Samae : un projet d'une autre
instance est refusé avec un message clair.

## 4. Vérifier

- `/etat` : « connexion GitLab active ».
- `/projet configurer projet:<référence>` → connexion → page avec URL et secret → webhook dans GitLab →
  **Tester** : notification dans le salon « Code ».
- `/projet diagnostic projet:<référence>` (si la PR de diagnostic est fusionnée) : chaque étape au vert.
- En cas d'échec côté GitLab : **Paramètres → Webhooks → Modifier → Événements récents** montre le code
  HTTP reçu (401 : mauvais secret ; 403 : projet différent ; 404 : Nginx ; 502 : Samae arrêté). GitLab
  désactive un webhook qui échoue plusieurs fois de suite : réactivez-le après correction.

Plan de test complet : [test-manuel.md, section 1.10.0](test-manuel.md).

## 5. Retour arrière

Vider `GITLAB_APP_CLIENT_ID` et `GITLAB_APP_CLIENT_SECRET`, redémarrer : la page `/gitlab/callback`
disparaît (404) et `/projet configurer projet:` répond que la connexion n'est pas activée. Les projets
déjà reliés continuent de recevoir leurs événements tant que leur webhook existe dans GitLab ; pour les
arrêter, supprimez le webhook dans GitLab (la déconnexion depuis Discord est l'étape B6). La migration
v12 ne fait qu'ajouter une table : une version 1.9.0 refuse de démarrer sur une base v12 (comme pour
toute migration), restaurez alors la sauvegarde `*-avantv12.sqlite3` faite automatiquement.

## 6. Ce qui est testé, et où

| Quoi | Statut |
| --- | --- |
| Parcours complet (lien, retour, rôle, révocation, secret affiché une fois, webhook accepté, notification, doublon) | ✅ tests locaux avec un faux GitLab (`tests/test_gitlab_app.py`) |
| Refus : rôle Developer, projet invisible, projet renommé, autorisation annulée, code réutilisé, lien rechargé, GitLab 500/429, projet supprimé, autre serveur, autre instance, doublon avec le `.env` | ✅ tests locaux |
| Secret et tokens absents de la base et des journaux | ✅ tests locaux |
| Vraie application OAuth, vrai GitLab, vrai HTTPS, vrai Discord | ⏳ nécessite le VPS et ton accord (section 2) |
