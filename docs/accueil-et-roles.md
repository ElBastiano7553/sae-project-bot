# Accueil des nouveaux membres, choix des rôles et messages permanents (1.4.0)

Ce guide décrit ce qui a changé en 1.4.0 pour l'arrivée des membres, et les manipulations à faire
dans Discord. Rien ne s'active tout seul de façon risquée : le message d'arrivée est désactivé par
défaut, et le choix des rôles ne peut jamais donner un rôle de staff.

## 1. Ce que voit un nouveau membre

1. **Message d'arrivée** (si `WELCOME_ON_JOIN=true`) : dans `#bienvenue`, Samae publie
   « Bienvenue @pseudo ! » avec trois étapes (lire le règlement, choisir ses rôles, se présenter)
   et un bouton **Choisir mes rôles**. Seul le nouveau membre est notifié. Le message est supprimé
   automatiquement après 24 heures (`WELCOME_DELETE_AFTER_HOURS`), ou dès que le membre quitte le
   serveur. Samae ne supprime que les messages d'accueil qu'il a lui-même publiés et notés : jamais
   un autre message du salon.
2. **Message de bienvenue permanent** (`#bienvenue`) : présentation du serveur, premiers pas, et le
   même bouton **Choisir mes rôles**.
3. **Panneau des rôles** (`#rôles`, ou `#bienvenue` si ce salon n'existe pas) : la liste des rôles
   proposés et à quoi ils servent, avec le bouton.
4. **`/roles`** : la même chose, à tout moment, depuis n'importe quel salon.

Le bouton et `/roles` ouvrent un **menu privé** (visible seulement par la personne) déjà coché avec
ses rôles actuels. On coche ou décoche plusieurs rôles, on ferme le menu : Samae ajoute et retire
les rôles en une fois et affiche ce qui a changé. On peut recommencer quand on veut.

## 2. Quels rôles sont proposés

Aucun nouveau système de rôles n'a été inventé : Samae reprend les rôles déjà décrits dans
`config/server.toml` (créés par `/refonte`) et marqués `self_assignable = true`.

| Rôle | Type | Proposé | Pourquoi |
|---|---|---|---|
| Développement | domaine | ✅ | code du jeu, outils, intégration |
| Game Design | domaine | ✅ | gameplay, règles, niveaux |
| Art & Design | domaine | ✅ | graphismes, interface, sons |
| Testeur | domaine | ✅ | tests des builds, bugs |
| Notif · Annonces, Notif · Réunions | notification | ✅ s'ils existent (rôles facultatifs) | être prévenu |
| Notif · Votes | notification | ❌ | le système de votes n'existe pas encore |
| Responsable projet, Modérateur | staff | ❌ jamais | attribués par un administrateur |
| Responsable de réunion | responsabilité | ❌ | attribué par un administrateur |

Garde-fous (vérifiés à l'ouverture du menu **et** au moment d'appliquer le choix) :

* `config/server.toml` refuse `self_assignable` sur un rôle autre que « fonction » ou
  « notification », ou sur un rôle qui porte une permission ;
* un rôle qui, sur Discord, porte une permission sensible (Administrateur, Gérer le serveur, les
  rôles, les salons, les messages, Mentionner @everyone, Expulser, Bannir…) est écarté, même s'il
  est marqué : un administrateur a pu le modifier à la main ;
* un rôle placé au-dessus du rôle de Samae, ou géré par une intégration, est écarté ;
* seuls les rôles proposés peuvent être ajoutés **ou retirés** : les rôles donnés par
  l'administration ne bougent jamais, même si la requête est trafiquée.

`/admin diagnostic` liste les rôles proposés et, pour chaque rôle écarté, la raison exacte.

**Rôles qui pourraient manquer** (proposition, rien n'est créé) : l'équipe a des salons de
livrables (`#documentation-et-preuves`, `#préparation-revues`) et d'audio (`#assets-et-audio`) sans
rôle de domaine dédié. Si c'est utile, ajoutez par exemple dans `config/server.toml` un rôle
`Documentation & livrables` (ou `Son & musique`) avec `kind = "fonction"` et `self_assignable = true`,
puis `/refonte lancer` le créera et `/admin actualiser` l'ajoutera au panneau.

## 3. Manipulations dans Discord

### Pour le choix des rôles (obligatoire)

1. Paramètres du serveur > **Rôles** : faites glisser le rôle de **Samae au-dessus** des rôles
   Développement, Game Design, Art & Design, Testeur et Notif · … (Discord interdit à un bot de
   donner un rôle placé au-dessus du sien). `/refonte lancer` place normalement les rôles dans le bon
   ordre ; vérifiez seulement que le rôle de Samae est le plus haut des deux.
2. Samae doit avoir la permission **Gérer les rôles** (elle lui est déjà nécessaire pour `/refonte`).
3. Facultatif : créez le salon `#rôles` avec `/refonte lancer facultatifs:True` (ou à la main dans la
   catégorie Accueil). Sans lui, le panneau des rôles est publié dans `#bienvenue`.
4. `/admin installer` puis **Confirmer** (ou `/admin actualiser`) : publie le panneau des rôles et le
   nouveau bouton du message de bienvenue.
5. `/admin diagnostic` : la section « Choix des rôles et accueil » doit montrer ✅ pour chaque rôle.

### Pour le message d'arrivée (facultatif)

Discord n'envoie les arrivées de membres qu'aux bots qui ont l'intent privilégié **Server Members**.

1. [Portail développeur Discord](https://discord.com/developers/applications) > votre application >
   **Bot** > *Privileged Gateway Intents* > activez **Server Members Intent** > **Save Changes**.
2. Dans `.env` sur le serveur qui héberge Samae : `WELCOME_ON_JOIN=true` (et, si vous voulez un autre
   délai, `WELCOME_DELETE_AFTER_HOURS=24` ; `0` = ne jamais supprimer, 168 au plus).
3. Redémarrez Samae (`sudo systemctl restart samae` sur le VPS).

Si l'intent n'est pas activé sur le portail, Samae refuse de démarrer et le journal l'explique
(« Discord refuse l'intent Server Members… ») : activez-le ou remettez `WELCOME_ON_JOIN=false`.
Le texte du message se modifie dans `content/accueil.toml` (relu à chaque arrivée).

## 4. Règlement et annonces : la cause de la confusion

**Constat.** Le salon `#annonces` contenait à la fois l'annonce en cours et les **règles de
l'équipe**, alors que le serveur a, depuis la refonte, un salon `#règlement` resté vide.

**Cause exacte** (indépendante des noms de salons) : le message permanent des règles était le
fichier `content/messages/annonces.toml`, qui visait la fonction de salon `annonces`
(`channel = "annonces"`). Ce choix datait de la V1, quand le serveur n'avait pas de salon dédié aux
règles ; il n'a pas été mis à jour quand la refonte a ajouté la fonction `regles` (`#règlement`).
Le message de bienvenue renvoyait d'ailleurs vers les règles « dans #annonces ».

**Correction à la source.**

* Le fichier s'appelle maintenant `content/messages/reglement.toml` et vise la fonction `regles`.
* `#règlement` étant facultatif, `config/server.toml` lui donne un **salon de repli**
  (`fallback = "annonces"`) : tant que `#règlement` n'existe pas, le règlement reste dans
  `#annonces` ; dès que `#règlement` est associé, Samae y **déplace** le message (nouveau message
  publié, puis l'ancien supprimé : jamais de doublon, jamais de trou).
* `previous_keys = ["annonces"]` : le message déjà publié sous l'ancien nom est **repris** (même
  message Discord, modifié sur place), pas republié.
* Les liens `{{#regles}}` des messages pointent vers le salon réellement utilisé (règlement, ou
  annonces en repli). Le même mécanisme sert au panneau des rôles (`#rôles`, repli `#bienvenue`).

## 5. Messages permanents après 1.4.0

| Message (`content/messages/`) | Salon | Rôle |
|---|---|---|
| `bienvenue` | `#bienvenue` | présentation, premiers pas, bouton **Choisir mes rôles** |
| `liens` | `#bienvenue` | liens utiles (générés depuis `config/links.toml`) |
| `roles` *(nouveau)* | `#rôles`, repli `#bienvenue` | rôles proposés et leur rôle, bouton |
| `reglement` *(était `annonces`)* | `#règlement`, repli `#annonces` | règles de l'équipe |
| `annonce-en-cours` | `#annonces` | annonce modifiée avec `/admin annonce` |
| `planning` | `#planning` | échéances de `config/planning.toml` |
| `guide-reunions` *(nouveau)* | `#réunions` | mode d'emploi court de `/reunion` |
| `decisions` | `#décisions` | comment documenter une décision |
| `documentation` | `#documentation-et-preuves` | documents et preuves pour l'EPITA |
| `tableau-de-bord` | `#activité-git` | tableau de bord GitLab ; explique que GitHub publie aussi ici |

Tous les pieds de page disent maintenant « Message géré par Samae ». Les messages publiés avec
l'ancien pied de page (« SAE Project Bot ») restent reconnus : ils sont modifiés sur place, jamais
dupliqués.

**Nouvelle installation** : `/admin installer` publie chaque message une fois. **Mise à jour d'un
serveur existant** : au premier `/admin actualiser` (ou au démarrage si `AUTO_REFRESH_ON_STARTUP=true`),
les messages existants sont modifiés sur place ; le règlement reste dans `#annonces` tant que
`#règlement` n'est pas associé, puis il y est déplacé ; les deux nouveaux panneaux (rôles, réunions)
sont publiés. Aucune table de la base n'est modifiée : seule la clé du message des règles est
renommée (`annonces` → `reglement`) dans la table des messages permanents, et la liste des messages
d'accueil à supprimer est gardée dans la table clé-valeur existante.
