# Architecture du SAE Project Bot

Ce document présente l'architecture retenue, les difficultés techniques, le flux
des données entre Discord, GitLab et l'IA, ainsi que le plan de réalisation.
Les lots 1 à 4 sont implémentés ; le déploiement réel sur le serveur reste à faire.

## 0. Architecture cible (lots 1 à 4)

```
            Discord (commandes, messages)                GitLab (webhooks HTTPS)
                       │                                          │
        src/discord_bot/ (adaptateurs discord.py)        src/gitlab/ (aiohttp, sans discord.py)
        cogs/general.py  /aide /etat /activite resume          webhook.py   authentification, taille, JSON
        cogs/activity.py /activite evenements /activite stats /activite contributions
        cogs/ai.py       /ia question /ia bilan /ia relecture
        cogs/redesign.py /refonte lancer|retour|journal (snapshot.py, redesign.py, redesign_ui.py)
        cogs/meetings.py /reunion … · help_ui.py /aide
        cogs/admin.py    /admin installer /admin actualiser /admin config         dispatcher.py dédup., file, historique
                         /admin annonce /admin …              events.py    JSON → Notification
        embeds.py ◄── services/design.py (couleurs, icônes, horodatages, limites)
                       │                                          │
                       └──────────► src/services/ ◄───────────────┘
                         permanent_messages (verrou, idempotence)   content (liens, annonce)
                         health · monitoring · diagnostics · help   history · dashboard · alerts
                                         │
                                 src/database/ (SQLite, migrations versionnées)
                                         │
                       src/ai/ : fournisseurs interchangeables (Claude, faux fournisseur), désactivables
                       src/gitlab/api.py : API GitLab en lecture seule (diffs pour /ia relecture)
```

| Lot | Contenu | État |
|---|---|---|
| 1 | Audit, design des embeds, messages permanents fiables, commandes de base | ✅ livré (version 0.3.0) |
| 2 | Historique GitLab consultable, statistiques, leaderboard, tableau de bord permanent, alertes | ✅ livré (version 0.4.0) |
| 3 | `src/ai/` multi-fournisseurs, `/ia question` (recherche documentaire simple), `/ia bilan`, `/ia relecture` | ✅ livré (version 0.5.0) |
| 4 | Fiabilité (arrêt propre, heartbeat), sauvegardes SQLite (`services/backup.py`), sécurité, Docker (`Dockerfile`, `docker-compose.yml`, `src/healthcheck.py`) et guide de déploiement Linux | ✅ livré (version 1.0.0) ; déploiement La Banquise à faire |
| Samae 2.0 · 1 | Réunions : `database/meetings.py`, `services/meetings.py`, `cogs/meetings.py`, `meeting_ui.py` (boutons persistants), migration v7 | ✅ livré (version 1.1.0) |
| 1.2.0 | Refonte du serveur (`services/server_redesign.py` pur, `discord_bot/redesign.py` exécuteur, `redesign_ui.py`, `cogs/redesign.py`), centre d'aide (`services/help.py`, `discord_bot/help_ui.py`), nouvelle palette ; pas de migration | ✅ livré, non déployé ([détails](refonte-et-aide.md)) |
| 1.2.1 | Exploitation : `src/instance_lock.py` (instance unique), `src/systemd_notify.py` (chien de garde), `src/ops.py` (sauvegarde, test de restauration, restauration, surveillance), `deploy/` (service systemd, mise à jour, retour arrière) ; synchronisation des commandes seulement si elles changent, mesures de réactivité (`InteractionStats`) ; pas de migration | ✅ livré, non déployé ([guide](deployment-vps.md)) |
| 1.3.0 | Refonte complète de `/refonte` : `services/redesign_target.py` (profils, styles, accès), `services/server_redesign.py` réécrit (inventaire, actions explicites, permissions, ordre, doublons, suppressions séparées), `services/redesign_rollback.py`, `discord_bot/snapshot.py` complet ; commandes `/refonte lancer`, `retour`, `journal` ; pas de migration | ✅ livré, non déployé ([détails](refonte.md)) |
| Samae 2.0 · 2 à 4 | Idées et tier lists, débat et communauté, finitions : voir [samae-2-plan.md](samae-2-plan.md) | ⏳ à faire |

Principes ajoutés au lot 1 :

- **Design centralisé** : `services/design.py` définit les couleurs (vert réussite,
  rouge échec, orange avertissement, bleu information, violet fusion, gris neutre),
  les icônes, les horodatages Discord (`<t:…>`) et les pieds de page ; `embeds.py`
  construit tous les embeds avec `make_embed` puis `fit_embed` (limites Discord).
- **Messages permanents fiables** : un verrou unique sérialise toutes les
  opérations ; la base n'est modifiée qu'après un succès Discord ; déplacement et
  reconstruction publient le nouveau message **avant** de supprimer l'ancien.
- **Pas de donnée inventée** : un lien vide reste « à configurer », `/activite resume`
  n'affiche que des événements réellement reçus, `/etat` dit ce qu'il ne vérifie pas.

## 1. Principes

1. **Séparer la logique métier de Discord.** Tout ce qui décide (quel salon
   correspond à quelle fonction, faut-il créer ou modifier un message…) vit dans
   `src/services/` et ne dépend pas de discord.py. On peut donc le tester sans
   token ni réseau. Le dossier `src/discord_bot/` ne fait que traduire entre
   Discord et ces services.
2. **Le contenu n'est pas du code.** Les textes des messages permanents sont dans
   `content/messages/*.toml`, la structure du serveur, les liens et le planning dans
   `config/*.toml`. Modifier un texte ne demande pas de toucher au Python.
3. **Idempotence partout.** Relancer une opération (redémarrage, `/admin actualiser`, webhook
   reçu deux fois) ne doit jamais produire de doublon. La base SQLite mémorise ce qui
   a déjà été fait.
4. **Prudence par défaut.** Le bot ne crée, ne renomme et ne supprime aucun salon,
   rôle ou message d'utilisateur. Il n'a pas besoin de la permission Administrateur.
   Les actions qui modifient le serveur passent par une prévisualisation suivie
   d'une confirmation.
5. **Chaque intégration est optionnelle.** Sans GitLab ou sans clé d'IA, le bot
   démarre et l'indique dans `/etat`.

## 2. Arborescence et responsabilités

```
sae-project-bot/
├── README.md                  Présentation et démarrage rapide
├── requirements.txt           Dépendances d'exécution (versions épinglées)
├── requirements-dev.txt       Dépendances de test
├── pyproject.toml             Réglages de pytest
├── .env.example               Modèle de configuration (sans secret)
├── config/
│   ├── server.toml            Structure attendue du serveur : fonction → nom du salon
│   ├── links.toml             Liens utiles (README, GitLab, journal IA…)
│   ├── gitlab.toml            (V2) Salons, branches et événements GitLab notifiés
│   └── planning.toml          Échéances et prochaines étapes (jamais inventées)
├── content/messages/          Un fichier TOML par message permanent
├── data/                      Base SQLite (ignorée par Git)
├── logs/                      Journaux du bot (ignorés par Git)
├── docs/                      Architecture, guides (Windows, webhooks GitLab, historique, IA)
├── knowledge/                 (lot 3) Notes de l'équipe consultées par /ia question
├── src/
│   ├── main.py                Point d'entrée : charge .env, vérifie la config, lance le bot
│   ├── config.py              Lecture et validation du .env et des fichiers TOML
│   ├── logging_setup.py       Journaux console + fichier
│   ├── database/
│   │   ├── connection.py      Connexion SQLite et migrations versionnées
│   │   ├── repositories.py    Seul endroit où l'on écrit du SQL métier
│   │   └── meetings.py        SQL des réunions, transactions avec révision (Samae 2.0)
│   ├── services/              Logique métier, sans dépendance à Discord
│   │   ├── server_structure.py   Reconnaissance des catégories et salons existants
│   │   ├── content.py            Chargement et rendu des messages permanents
│   │   ├── permanent_messages.py Publication idempotente (création / mise à jour / recréation / déplacement)
│   │   ├── design.py             Identité visuelle : couleurs, icônes, horodatages (lot 1)
│   │   ├── health.py             Rapport d'état pour /etat
│   │   ├── monitoring.py         Dernières erreurs, masquage des secrets dans les journaux (lot 1)
│   │   ├── diagnostics.py        Permissions requises par salon (lot 1)
│   │   ├── help.py               Catalogue de /aide, vérifié par les tests (lot 1)
│   │   ├── history.py            Statistiques et classement calculés sur l'historique (lot 2)
│   │   ├── dashboard.py          Contenu du tableau de bord de #gitlab-activite (lot 2)
│   │   ├── alerts.py             Regroupement des erreurs et décision d'alerte (lot 2)
│   │   ├── knowledge.py          Recherche des passages utiles pour /ia question (lot 3)
│   │   ├── ai_assistant.py       Consignes, quotas, masquage des secrets, journal des demandes (lot 3)
│   │   ├── meetings.py           Réunions : règles, permissions, compte rendu, limites (Samae 2.0)
│   │   ├── self_roles.py         Rôles que les membres peuvent choisir, garde-fous (1.4.0)
│   │   ├── welcome.py            Message d'arrivée et suppression automatique (1.4.0)
│   │   └── text_limits.py        Respect des limites de taille de Discord
│   ├── discord_bot/           Tout ce qui dépend de discord.py
│   │   ├── bot.py             Client, synchronisation des commandes, événements
│   │   ├── cogs/general.py    Commandes /aide et /etat (lot 1)
│   │   ├── cogs/activity.py   Groupe /activite : resume, evenements, stats, contributions (lot 2)
│   │   ├── cogs/ai.py         Groupe /ia : question, bilan, relecture (lot 3)
│   │   ├── cogs/welcome.py    /roles, bouton « Choisir mes rôles », message d'arrivée (1.4.0)
│   │   ├── roles_ui.py        Menu privé du choix des rôles, bouton persistant (1.4.0)
│   │   ├── cogs/meetings.py   Groupe /reunion et boutons du panneau (Samae 2.0)
│   │   ├── meeting_ui.py      Panneau, compte rendu paginé, boutons persistants (Samae 2.0)
│   │   ├── alerts.py          Publication des alertes dans ALERTS_CHANNEL (lot 2)
│   │   ├── cogs/admin.py      Groupe /admin : installer, actualiser, reconstruire, messages, annonce, config, diagnostic…
│   │   ├── gateway.py         Opérations Discord utilisées par les services
│   │   ├── snapshot.py        Conversion du serveur Discord en données simples
│   │   ├── embeds.py          Mise en forme des embeds
│   │   ├── gitlab_sender.py   (V2) Publication des notifications GitLab (aucune mention)
│   │   ├── views.py           Bouton de confirmation avec expiration, formulaire d'annonce
│   │   └── permissions.py     Qui peut utiliser les commandes d'administration
│   ├── gitlab/                (V2) Sans dépendance à discord.py
│   │   ├── webhook.py         Serveur aiohttp : secret, JSON, taille, projet, réponses HTTP
│   │   ├── dispatcher.py      Déduplication, file d'attente, envoi en tâche de fond
│   │   ├── events.py          JSON GitLab → notification (push, MR, pipeline, déploiement)
│   │   ├── rules.py           Lecture de config/gitlab.toml
│   │   ├── api.py             (lot 3) API GitLab en lecture seule : MR et diffs
│   │   ├── security.py        Comparaison du secret, nettoyage des textes et des URL
│   │   ├── samples.py         Exemples de JSON GitLab (tests et démonstration)
│   │   └── demo.py            python -m src.gitlab.demo : faux webhook pour tester
│   ├── ai/                    (lot 3) Sans dépendance à discord.py
│   │   ├── base.py            Interface commune AIProvider, erreurs lisibles
│   │   ├── anthropic_provider.py  Claude via le SDK officiel anthropic
│   │   ├── fake.py            Faux fournisseur (tests, AI_PROVIDER=fake)
│   │   └── factory.py         Choix du fournisseur d'après AI_PROVIDER
└── tests/                     Tests pytest (aucun vrai token nécessaire)
```

Le dossier `src/ai/` sera créé à son étape (lot 3) pour ne pas livrer de code vide.

### Séparation demandée

| Responsabilité                 | Où ?                                               |
|--------------------------------|----------------------------------------------------|
| Commandes Discord              | `src/discord_bot/cogs/`                            |
| Gestion des messages permanents| `src/services/permanent_messages.py` + `content.py`|
| Événements GitLab (V2)         | `src/gitlab/`                                      |
| Accès à la base de données     | `src/database/`                                    |
| Intégration IA (V4)            | `src/ai/`                                          |
| Logique métier                 | `src/services/`                                    |
| Configuration                  | `src/config.py`, `config/`, `.env`                 |

## 3. Flux des données

### V1 : messages permanents

```
/admin installer (admin)
   │
   ▼
snapshot du serveur ──► server_structure.resolve_structure ◄── config/server.toml
   │                         │                                  + identifiants déjà enregistrés
   │                         ▼
   │                 rapport : salons OK / absents / ambigus
   ▼
permanent_messages.plan  ◄── content/messages/*.toml + config/links.toml + config/planning.toml
   │
   ▼
Prévisualisation (embed éphémère) + bouton « Confirmer » (2 minutes)
   │ confirmé
   ▼
SQLite : channel_bindings ◄── associations fonction → salon
permanent_messages.sync ──► Discord (envoyer / modifier / épingler)
   │
   ▼
SQLite : managed_messages (clé, salon, message, empreinte du contenu)
```

Pour chaque message, le service compare l'empreinte (SHA-256) du contenu attendu
avec celle enregistrée : rien n'est envoyé si rien n'a changé, et un contenu modifié
est **édité sur place** (même lien, réactions conservées). Un message supprimé à la
main est recréé automatiquement quelques secondes plus tard (événement Discord de
suppression), ou au prochain `/admin actualiser`. Si la base est perdue, le bot retrouve ses
anciens messages grâce à la référence écrite dans leur pied de page et les reprend
au lieu d'en publier de nouveaux.

Toutes les opérations (`/admin installer`, `/admin actualiser`, actualisation au démarrage, recréation
automatique, `/admin annonce`, `/admin reconstruire`) passent par **un seul verrou** : deux
actualisations simultanées s'exécutent l'une après l'autre et la seconde trouve les
messages à jour. Si le salon d'un message change, le nouveau message est publié et
enregistré, puis l'ancien message du bot est supprimé (aucun doublon laissé
volontairement) ; si cette suppression échoue, le rapport demande de le faire à la main.
Les liens vides de `config/links.toml` ne sont jamais remplacés par une adresse
inventée : le message affiche une section « À compléter » qui distingue les liens
obligatoires (`required`) des facultatifs, et disparaît dès qu'ils sont renseignés.

Limite connue : le verrou protège un seul processus. Lancer deux instances du bot
avec la même base reste à éviter (voir deployment-linux.md : une seule instance).

### V2 : notifications GitLab (implémentée, à valider avec le vrai GitLab)

```
GitLab ──HTTPS──► tunnel (Cloudflare / ngrok) ──► aiohttp 127.0.0.1:8080/webhooks/gitlab
                                                     │ (même processus que le bot)
   1. X-Gitlab-Token comparé au secret à temps constant, avant de lire le corps → sinon 401
   2. Content-Type JSON (415), 2 Mo max (413), objet JSON valide (400)
   3. X-Gitlab-Event : Push / Merge Request / Pipeline Hook ; autre → 200 « ignored »
   4. project.id = GITLAB_PROJECT_ID si défini → sinon 403
   5. events.format_* : filtres de config/gitlab.toml, textes nettoyés et tronqués
   6. SQLite gitlab_events : clé Idempotency-Key ou X-Gitlab-Event-UUID déjà vue → « duplicate » ;
      même fait GitLab sous un autre identifiant (empreinte dedup_key, index unique) → « duplicate »
   7. file asyncio (100 max, sinon 503 et l'événement est oublié pour que GitLab réessaie) ;
      la notification est enregistrée avec la clé, pour être republiée si le bot s'arrête avant l'envoi
                                                     │ réponse 200 immédiate à GitLab
                                                     ▼
   tâche de fond ──► channel_bindings (salon associé au /admin installer) ──► Discord
                     3 essais (0, 2, 10 s) ; statut enregistré : sent / failed / skipped
                     au démarrage : notifications « queued » de moins de 24 h remises en file
```

Le serveur ne démarre que si `GITLAB_WEBHOOK_SECRET` est défini. S'il ne peut pas
démarrer (variable `GITLAB_*`/`WEBHOOK_*` invalide, port occupé, `config/gitlab.toml`
invalide, erreur inattendue), l'erreur est journalisée et affichée par `/etat`,
mais la V1 continue de fonctionner.

### V3 et V4 : API GitLab et IA (prévu)

```
/bilan, /avancement… ──► API GitLab (token lecture seule) ──► données brutes
                                                          │
                                         (V4, facultatif) ▼
                                     fournisseur IA : reçoit uniquement des données
                                     filtrées, présentées comme des données et jamais
                                     comme des instructions ; la réponse cite les liens
                                     GitLab et distingue faits et hypothèses
```

Le contenu des commits, Issues et Merge Requests est toujours traité comme une
donnée non fiable : il est échappé avant l'affichage et encadré comme « données »
dans les requêtes à l'IA, pour résister aux tentatives d'injection de consignes.

## 4. Difficultés techniques et dépendances

| Difficulté | Réponse retenue |
|---|---|
| Retrouver des salons dont le nom contient emojis et accents | Normalisation (suppression des accents, emojis, séparateurs) ; un salon déjà associé est retrouvé par son identifiant même s'il est renommé. |
| Deux salons avec le même nom | Préférence pour la catégorie attendue ; sinon le bot signale l'ambiguïté et ne choisit pas au hasard. |
| Ne pas publier de doublon au redémarrage | Table `managed_messages` + empreinte du contenu + recherche par référence en pied de page si la base est perdue. |
| `/admin installer confirm` n'est pas une syntaxe possible | Discord n'autorise pas une commande à la fois directe et avec sous-commandes. `/admin installer` affiche donc la prévisualisation avec un bouton « Confirmer » qui expire après 2 minutes et n'est utilisable que par son auteur. |
| Commandes slash lentes à apparaître | Synchronisation sur le seul serveur configuré : apparition immédiate (la synchronisation globale prend jusqu'à une heure). |
| Limites de taille de Discord (embed 6000 caractères, champ 1024…) | Module `text_limits` : troncature propre et découpage en plusieurs champs. |
| Recevoir des webhooks depuis Internet sur un PC Windows (V2) | Le PC n'est pas joignable depuis gitlab.com : le serveur écoute sur 127.0.0.1 et un tunnel (Cloudflare Quick Tunnel, ngrok ou Cloudflare Tunnel nommé) fournit l'adresse HTTPS publique. Voir [gitlab-webhooks.md](gitlab-webhooks.md). |
| Webhook reçu deux fois (V2) | Table `gitlab_events`, clé = `Idempotency-Key` (stable entre les tentatives de GitLab) ou, à défaut, `X-Gitlab-Event-UUID`, ou l'empreinte du corps. |
| GitLab désactive un webhook qui échoue souvent (V2) | Réponse 200 « ignored » pour les événements authentifiés mais non gérés ; envoi Discord hors de la requête pour répondre en moins de 10 secondes. |
| Messages de commit écrits par n'importe qui (V2) | Markdown échappé, mentions neutralisées, URL validées, `allowed_mentions` désactivé à l'envoi. |
| Coûts et limites de l'IA (V4) | Fenêtre temporelle explicite, plafonds de taille, cache des résumés, nombre d'appels limité, désactivation possible avec `AI_PROVIDER=none`. |
| SQLite dans un programme asynchrone | Requêtes minuscules et rapides : `sqlite3` standard suffit, sans dépendance supplémentaire. |

**Dépendances (versions épinglées et testées ensemble)** : `discord.py 2.7.1`
(commandes slash, boutons), `aiohttp 3.14.5` (déjà requis par discord.py, utilisé
pour le serveur de webhooks de la V2), `python-dotenv 1.2.4`, `pytest 9.1.1` et `pytest-asyncio 1.4.0`.
Python **3.12** est recommandé ; les tests passent aussi sous 3.11 et 3.13.

## 5. Plan de réalisation

| Étape | Contenu | Critère de validation |
|---|---|---|
| **V1** (livrée) | Bot Discord, `/etat`, `/admin config`, `/admin installer` avec confirmation, `/admin actualiser`, messages permanents idempotents, SQLite, tests, guide Windows | `/admin installer` reconnaît les 19 salons ; deux `/admin actualiser` successifs ne créent aucun doublon |
| **V2** (implémentée et testée automatiquement ; validation sur le vrai GitLab à faire) | Serveur de webhooks GitLab (push, MR, pipelines ; Issues et commentaires reportés), secret, idempotence, nouvelle table SQLite, redirection vers les salons, guide de configuration du webhook | Un « Test webhook » GitLab apparaît une seule fois dans le bon salon ; un mauvais secret est refusé |
| **V3** | Client API GitLab en lecture seule ; `/avancement`, `/bugs`, `/merge-requests`, `/bilan` fondés sur les données brutes ; bilan hebdomadaire planifié sans répétition au redémarrage | Les chiffres affichés correspondent à GitLab ; aucun bilan en double |
| **V4** | Interface `AIProvider` (fournisseur configurable, désactivable) ; `/resume-commit`, `/aide-projet`, synthèses ; journal IA volontaire avec export Markdown pour le SAE | Le bot fonctionne sans clé ; les réponses citent leurs liens et signalent les données manquantes |
| **V5** | Tests complets, durcissement sécurité, documentation finale, préparation à l'usage quotidien (service Windows ou hébergement) | Revue de sécurité et checklist d'exploitation |

Depuis octobre 2026, la suite du travail est organisée en **lots** (section 0) :
les lots 1 à 4 sont livrés ; ils reprennent et précisent les étapes V3 à V5.

Chaque étape s'appuie sur la précédente sans la réécrire : de nouveaux modules
(`src/gitlab/`, `src/ai/`), de nouvelles migrations SQLite ajoutées à la fin de la
liste et de nouveaux cogs.

## 6. Schéma de la base (version 7)

| Table | Rôle |
|---|---|
| `managed_messages` | clé du message, salon, identifiant du message, empreinte du contenu, date ; (v4) dernière action, dernière erreur, dernière vérification |
| `channel_bindings` | fonction du bot → identifiant du salon (enregistré au `/admin installer` confirmé) |
| `kv_store` | état divers : date du dernier `/admin installer` confirmé, annonce en cours (JSON), branche principale indiquée par GitLab |
| `gitlab_events` | (V2) clé de livraison GitLab, type, statut (`queued`, `sent`, `failed`, `skipped`), notification en JSON (migration 3), date ; (v4) empreinte anti-doublon `dedup_key` (index unique), action, branche, titre, lien, auteur, date de l'événement ; (v5) `source`, `event_at` (date UTC normalisée), identifiants GitLab du membre, de la MR / du pipeline / du déploiement et de l'auteur de la MR, nombre de commits, environnement, statut `recorded` ; conservée `HISTORY_RETENTION_DAYS` jours (365 par défaut) |
| `admin_actions` | (v4) journal des actions d'administration : date, identifiant et nom du membre, action, cible, résultat |
| `gitlab_users` | (v5) identifiant GitLab → dernier nom et pseudo reçus (affichage de `/activite contributions`) |
| `ai_requests` | (v6) une ligne par demande à l'IA : date, membre, commande, modèle, tokens, résultat (jamais le texte) |
| `meetings`, `meeting_participants`, `meeting_steps`, `meeting_notes`, `meeting_actions`, `meeting_log` | (v7, Samae 2.0) réunions, participants, ordre du jour, décisions et réserves, actions, historique ; détail dans [meetings.md](meetings.md) |
| `bot_errors` | (v5) erreurs regroupées par empreinte : source, dernier message, première et dernière occurrence, nombre, date de la dernière alerte |

Migrations : chaque migration est exécutée dans **une seule transaction** avec la
mise à jour de `user_version` ; en cas d'erreur, la base reste intacte dans sa
version précédente et la migration est retentée au démarrage suivant. La
migration 4 n'ajoute que des colonnes, un index et une table : aucune donnée
existante n'est modifiée ni supprimée (le titre et le lien des événements V2 sont
recopiés depuis leur notification JSON). La migration 5 ajoute des colonnes, des
index et deux tables, et remplit seulement la nouvelle colonne `event_at` à partir
des dates existantes. La migration 7 ne crée que des tables. Une base plus récente que le code est
refusée au démarrage plutôt qu'utilisée à l'aveugle.

La version du schéma est stockée dans `PRAGMA user_version` ; les migrations sont
appliquées automatiquement au démarrage.
