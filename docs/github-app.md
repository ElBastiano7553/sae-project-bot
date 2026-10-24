# Connexion des projets GitHub par la GitHub App (étape B4, version 1.9.0)

`/projet configurer` relie un dépôt GitHub à Samae **sans aucun token dans Discord** : l'administrateur
installe la GitHub App de Samae sur le dépôt, GitHub confirme qu'il gère bien cette installation, puis
il choisit le dépôt dans un menu. Les événements de ce dépôt arrivent ensuite par la réception commune
(étape B3, [projets-reception.md](projets-reception.md)) et sont publiés dans les salons du projet.

**Rien n'est activé par défaut.** Tant que les variables ci-dessous sont vides, `/projet configurer`
répond que la connexion n'est pas activée, et tout le reste de Samae fonctionne comme en 1.8.0.
Les webhooks historiques du `.env` (ports 8080 et 8081) ne changent pas.

## 1. Le parcours

1. Un administrateur lance `/projet configurer`. Samae répond (visible par lui seul) avec un bouton
   **Installer sur GitHub** et un bouton **Choisir le dépôt**. Le lien d'installation porte un jeton
   personnel : signé, valable 15 minutes, utilisable une seule fois, lié à ce serveur Discord et à
   cette personne.
2. Sur GitHub, la personne choisit le compte et le ou les dépôts, puis valide. GitHub la renvoie sur
   `https://<adresse de Samae>/github/setup`.
3. Samae vérifie le jeton, puis envoie la personne autoriser Samae sur GitHub (un second jeton, lié en
   plus au numéro d'installation). GitHub la renvoie sur `/github/callback`.
4. Samae échange le code reçu contre un accès **temporaire** de la personne, demande à GitHub si elle a
   bien accès à cette installation et sur quels dépôts elle a le rôle **Admin ou Maintain**, puis
   **révoque aussitôt** cet accès, que la vérification réussisse ou non. Il ne garde que le numéro
   d'installation, le nom du compte et ces dépôts-là, pour **ce serveur Discord seulement**. Sans cette
   étape, quelqu'un pourrait relier l'installation d'un autre en changeant le numéro dans l'adresse.
   Un accès en lecture ou en écriture (Read, Triage, Write) ne suffit pas : un tel dépôt n'est pas proposé.
5. De retour sur Discord, **Choisir le dépôt** ouvre un menu (ou `/projet configurer depot:equipe/depot`).
   Un récapitulatif demande confirmation. Si le projet existait déjà (ajouté avec `/projet ajouter`), il
   est relié ; sinon il est créé puis relié. Son état devient « connecté : notifications actives ».
6. Si le projet a déjà des salons (`/projet modifier`), le bouton **Envoyer un message de test** publie
   un message dans chacun, après avoir vérifié que le salon appartient à ce serveur et que Samae peut y
   écrire. Un envoi n'est annoncé réussi que si Discord renvoie le message publié **dans ce salon de ce
   serveur** ; sinon le salon est listé dans « Impossible de publier ».

Chaque bouton revérifie, côté bot, que la personne est toujours administratrice de ce serveur. Un membre
ne voit pas la commande, et l'autocomplétion de `depot` ne lui propose rien.

### Ce que Samae ne fait pas

- Il ne détient **aucune clé privée** de l'App et n'a **aucun droit d'écriture** sur GitHub.
- Il ne garde **aucun token** : l'accès temporaire est révoqué dès la vérification faite.
- Les jetons des liens ne sont gardés que sous forme d'empreinte SHA-256, et sont **supprimés dès leur
  utilisation**, réussie ou non. Deux requêtes simultanées avec le même lien : une seule passe
  (suppression conditionnelle, atomique dans SQLite). Un lien expiré est refusé à 15 minutes pile, puis supprimé.
- En cas d'échec (lien invalide, autorisation annulée, code refusé, droits insuffisants, GitHub en panne),
  **rien n'est enregistré** et l'accès temporaire éventuel est révoqué.
- Les pages de retour ne sont pas mises en cache, n'envoient pas leur adresse à d'autres sites
  (`Referrer-Policy: no-referrer`) et n'affichent jamais de secret.
- Samae ne journalise pas les adresses reçues (`access_log=None`), et l'exemple Nginx retire les
  paramètres (`state`, `code`) de son journal.
- Le dépôt suivi par `GITHUB_WEBHOOK_SECRET` (projet historique `env-github`) est refusé tant que ce
  webhook historique est actif : sinon chaque événement serait publié deux fois.

## 2. Prérequis (avec l'accord du propriétaire du VPS)

| Prérequis | Pourquoi | Où |
| --- | --- | --- |
| Sous-domaine HTTPS (ex. `samae.exemple.fr`) avec certificat | GitHub n'envoie ses événements et ne renvoie la personne que vers une adresse HTTPS publique | [projets-reception.md, section 4](projets-reception.md) |
| Site Nginx `deploy/nginx/samae.conf.example` | ne laisse passer que `POST /github/webhook`, `GET /github/setup`, `GET /github/callback` (et le webhook GitLab de B5) | même guide |
| Réception des projets active | `PROJECTS_HTTP_ENABLED=true`, `GITHUB_APP_WEBHOOK_SECRET` | `.env` |
| GitHub App **de test** | permissions minimales, installée sur **un seul dépôt de test** | section 3 |
| Un dépôt GitHub de test | pour pousser des commits sans gêner l'équipe | GitHub |

**Je n'ai pas accès au VPS et rien n'y a été modifié** : ni Nginx, ni DNS, ni certificat. Ces étapes
sont à lancer par le propriétaire du VPS, quand il le décide. Sans elles, B4 reste testable de bout en
bout avec les tests automatiques et l'outil de contrôle local (section 5).

## 3. Créer la GitHub App de test (une fois, sur GitHub)

GitHub > photo de profil > **Settings** > **Developer settings** > **GitHub Apps** > **New GitHub App**.
Les valeurs ci-dessous sont aussi dans [`deploy/github-app/samae-test-app.json`](../deploy/github-app/samae-test-app.json)
(référence vérifiée par les tests) ; remplacez `samae.exemple.fr` par le vrai sous-domaine.

| Champ | Valeur |
| --- | --- |
| GitHub App name | `Samae Test` (nom libre, unique sur GitHub) |
| Homepage URL | `https://samae.exemple.fr` |
| Callback URL | `https://samae.exemple.fr/github/callback` |
| Expire user authorization tokens | coché |
| Request user authorization (OAuth) during installation | **décoché** |
| Setup URL | `https://samae.exemple.fr/github/setup` |
| Redirect on update | **coché** |
| Webhook > Active | coché |
| Webhook URL | `https://samae.exemple.fr/github/webhook` |
| Webhook secret | une longue valeur aléatoire (voir ci-dessous), **différente** de `GITHUB_WEBHOOK_SECRET` |
| Permissions > Repository | Contents : **Read-only** · Metadata : **Read-only** · Pull requests : **Read-only** · Issues : **Read-only** · Actions : **Read-only** |
| Permissions > Organization / Account | aucune |
| Subscribe to events | Push, Pull request, Issues, Workflow run |
| Where can this GitHub App be installed? | **Only on this account** |

Contents en lecture est nécessaire pour recevoir les `push` ; Actions pour les échecs de CI. Aucune
permission d'écriture.

Après la création :

1. Notez le **nom court** de l'App (dans son adresse `github.com/apps/<nom-court>`) et son **Client ID**.
2. **Generate a new client secret** : copiez-le **directement dans le `.env` du VPS**, jamais ailleurs.
3. **Ne générez pas de clé privée** : Samae n'en a pas besoin.
4. Ne l'installez pas encore : l'installation se fait depuis `/projet configurer`, et **uniquement sur
   le dépôt de test** (« Only select repositories »).

Pour générer le secret du webhook sur le VPS, sans l'afficher ailleurs que dans votre terminal :

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

## 4. Réglages (`.env` du VPS, jamais dans Discord)

```dotenv
PROJECTS_HTTP_ENABLED=true
GITHUB_APP_WEBHOOK_SECRET=<secret du webhook de l'App>
# Connexion des projets GitHub (B4) : les quatre sont nécessaires.
PROJECTS_PUBLIC_URL=https://samae.exemple.fr
GITHUB_APP_SLUG=samae-test
GITHUB_APP_CLIENT_ID=<Client ID de l'App>
GITHUB_APP_CLIENT_SECRET=<client secret de l'App>
```

- `PROJECTS_PUBLIC_URL` : `https://`, sans chemin ni identifiants.
- `GITHUB_APP_CLIENT_SECRET` est masqué dans les journaux, comme les autres secrets, et doit être
  différent des secrets des webhooks.
- Une erreur dans ces variables désactive **seulement** la connexion : `/etat` l'indique à la ligne
  « Réception des projets », le reste du bot démarre normalement.

Redémarrez Samae (`sudo systemctl restart samae`). `/etat` doit afficher « connexion GitHub active ».

## 5. Tests de bout en bout

### Automatiques (aucun réseau)

`tests/test_github_app.py` suit le vrai parcours avec de vraies requêtes HTTP sur des ports locaux et
un faux GitHub local : lien de `/projet configurer`, page `/github/setup`, autorisation, page
`/github/callback`, choix du dépôt et confirmation, puis événements signés reçus par B3.

- **Signatures** : absente, fausse, faite avec le secret OAuth ou l'ancien secret → 401.
- **Doublons** : même livraison renvoyée, même fait sous une autre livraison → une seule notification.
- **Filtres** : branche suivie publiée, branche écartée notée `filtered` sans message.
- **Isolation** : installation vérifiée pour un autre serveur jamais proposée ni utilisable ici ;
  même dépôt relié dans deux serveurs → chacun son salon ; salon d'un autre serveur jamais utilisé ;
  autre dépôt de la même installation ou autre installation → ignoré.
- **Refus** : lien réutilisé, expiré ou falsifié, jeton d'une étape utilisé à l'autre, numéro
  d'installation changé dans l'adresse, personne sans accès ou sans rôle Admin/Maintain, autorisation
  annulée, GitHub injoignable ; requêtes simultanées avec le même lien (une seule passe).
- **Données temporaires** : aucune ligne en attente ni installation après un échec, accès temporaire révoqué.
- **Secrets** : aucun token, code ou secret dans les pages, les journaux, la base ni Discord.
- **Discord** : membres refusés, autocomplétion vide pour eux, boutons revérifiés, dépôt du `.env`
  refusé, message de test refusé si Samae ne peut pas publier.

### Contrôle sur le serveur, sans GitHub ni HTTPS

Une fois le dépôt de test connecté et son salon « Code » choisi, sur le VPS (le secret est lu dans
`.env` et jamais affiché ; seule l'adresse locale est contactée) :

```bash
python -m src.providers.check --depot equipe/depot-de-test --installation 12345678 --filtre
```

Le numéro d'installation est à la fin de l'adresse de la page de l'installation sur GitHub
(Settings > Applications > Configure). `--filtre` suppose un filtre de branches sur la catégorie Code
(par exemple `/projet modifier categorie:Code branches:main`). Chaque ligne doit être ✅, et **un seul**
message « Contrôle de bout en bout » doit arriver dans le salon.

### Avec la vraie App de test (une fois le HTTPS en place)

Voir [test-manuel.md, section 1.9.0](test-manuel.md) : installation sur le dépôt de test, push sur
`main` et sur une branche filtrée, pull request, ticket, nouvelle livraison depuis GitHub (Advanced >
Recent Deliveries > Redeliver : doit être un doublon), et refus d'un second serveur.

## 6. Retour arrière

- Vider `GITHUB_APP_CLIENT_SECRET` (ou les quatre variables) et redémarrer : `/projet configurer` est
  désactivée, les projets déjà reliés continuent de recevoir leurs événements.
- Pour tout arrêter : `PROJECTS_HTTP_ENABLED=false` et redémarrage.
- Sur GitHub : désinstaller l'App du dépôt de test (Settings > Applications), ou la supprimer.
  La détection automatique de la désinstallation et `/projet deconnecter` arrivent avec l'étape B6.

## 7. Base de données (migration v11)

Création de deux tables, rien d'existant n'est modifié (sauvegarde automatique avant la migration) :

- `github_setup_states` : liens en attente seulement (empreinte SHA-256 du jeton, étape, serveur,
  personne, expiration). Une ligne est supprimée dès que le lien est utilisé ou qu'il expire.
- `github_installations` : par serveur Discord, installation vérifiée, compte, dépôts que la personne
  administre ou maintient (lus au moment de la vérification), administrateur Discord et date.

La liste des dépôts est celle du moment de la vérification : après avoir ajouté un dépôt à
l'installation sur GitHub, relancez `/projet configurer` et refaites le lien pour la mettre à jour.
