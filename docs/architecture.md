# Architecture du SAE Project Bot

Ce document présente l'architecture retenue, les difficultés techniques, le flux
des données entre Discord, GitLab et l'IA, ainsi que le plan de réalisation.
Les parties marquées **(V2+)** ne sont pas encore implémentées.

## 1. Principes

1. **Séparer la logique métier de Discord.** Tout ce qui décide (quel salon
   correspond à quelle fonction, faut-il créer ou modifier un message…) vit dans
   `src/services/` et ne dépend pas de discord.py. On peut donc le tester sans
   token ni réseau. Le dossier `src/discord_bot/` ne fait que traduire entre
   Discord et ces services.
2. **Le contenu n'est pas du code.** Les textes des messages permanents sont dans
   `content/messages/*.toml`, la structure du serveur, les liens et le planning dans
   `config/*.toml`. Modifier un texte ne demande pas de toucher au Python.
3. **Idempotence partout.** Relancer une opération (redémarrage, `/refresh`, webhook
   reçu deux fois) ne doit jamais produire de doublon. La base SQLite mémorise ce qui
   a déjà été fait.
4. **Prudence par défaut.** Le bot ne crée, ne renomme et ne supprime aucun salon,
   rôle ou message d'utilisateur. Il n'a pas besoin de la permission Administrateur.
   Les actions qui modifient le serveur passent par une prévisualisation suivie
   d'une confirmation.
5. **Chaque intégration est optionnelle.** Sans GitLab ou sans clé d'IA, le bot
   démarre et l'indique dans `/health`.

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
│   └── planning.toml          Échéances et prochaines étapes (jamais inventées)
├── content/messages/          Un fichier TOML par message permanent
├── data/                      Base SQLite (ignorée par Git)
├── logs/                      Journaux du bot (ignorés par Git)
├── docs/                      Architecture, guide Windows
├── src/
│   ├── main.py                Point d'entrée : charge .env, vérifie la config, lance le bot
│   ├── config.py              Lecture et validation du .env et des fichiers TOML
│   ├── logging_setup.py       Journaux console + fichier
│   ├── database/
│   │   ├── connection.py      Connexion SQLite et migrations versionnées
│   │   └── repositories.py    Seul endroit où l'on écrit du SQL métier
│   ├── services/              Logique métier, sans dépendance à Discord
│   │   ├── server_structure.py   Reconnaissance des catégories et salons existants
│   │   ├── content.py            Chargement et rendu des messages permanents
│   │   ├── permanent_messages.py Publication idempotente (création / mise à jour / recréation)
│   │   ├── health.py             Rapport d'état pour /health
│   │   └── text_limits.py        Respect des limites de taille de Discord
│   ├── discord_bot/           Tout ce qui dépend de discord.py
│   │   ├── bot.py             Client, synchronisation des commandes, événements
│   │   ├── cogs/admin.py      Commandes /setup, /refresh, /config, /health
│   │   ├── gateway.py         Opérations Discord utilisées par les services
│   │   ├── snapshot.py        Conversion du serveur Discord en données simples
│   │   ├── embeds.py          Mise en forme des embeds
│   │   ├── views.py           Bouton de confirmation avec expiration
│   │   └── permissions.py     Qui peut utiliser les commandes d'administration
│   ├── gitlab/                (V2) Serveur de webhooks, validation, client API lecture seule
│   └── ai/                    (V4) Interface commune des fournisseurs d'IA
└── tests/                     Tests pytest (aucun vrai token nécessaire)
```

Les dossiers `src/gitlab/` et `src/ai/` seront créés à leur étape : ils ne sont pas
présents dans la V1 pour ne pas livrer de code vide.

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
/setup (admin)
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
avec celle enregistrée : rien n'est envoyé si rien n'a changé. Un message supprimé
est détecté (`fetch_message` → introuvable) et recréé au prochain `/refresh`.
Si la base est perdue, le bot retrouve ses anciens messages grâce à la référence
écrite dans leur pied de page et les reprend au lieu d'en publier de nouveaux.

### V2 à V4 : GitLab et IA (prévu)

```
GitLab ──webhook HTTPS──► serveur aiohttp (même processus que le bot)
                            │ 1. vérifie X-Gitlab-Token (comparaison à temps constant)
                            │ 2. valide le JSON et le type d'événement
                            │ 3. idempotence : X-Gitlab-Event-UUID déjà vu ? → ignoré
                            │ 4. masque les secrets éventuels, tronque les textes
                            ▼
                    file d'attente interne ──► Discord (#gitlab-activite, #builds-et-tests…)

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
| `/setup confirm` n'est pas une syntaxe possible | Discord n'autorise pas une commande à la fois directe et avec sous-commandes. `/setup` affiche donc la prévisualisation avec un bouton « Confirmer » qui expire après 2 minutes et n'est utilisable que par son auteur. |
| Commandes slash lentes à apparaître | Synchronisation sur le seul serveur configuré : apparition immédiate (la synchronisation globale prend jusqu'à une heure). |
| Limites de taille de Discord (embed 6000 caractères, champ 1024…) | Module `text_limits` : troncature propre et découpage en plusieurs champs. |
| Recevoir des webhooks depuis Internet sur un PC Windows (V2) | Le PC n'est pas joignable depuis gitlab.com : il faudra un tunnel (par exemple Cloudflare Tunnel ou ngrok) ou un petit hébergement. Le guide V2 détaillera les options. |
| Webhook reçu deux fois (V2) | Table des événements traités, clé = UUID fourni par GitLab. |
| Coûts et limites de l'IA (V4) | Fenêtre temporelle explicite, plafonds de taille, cache des résumés, nombre d'appels limité, désactivation possible avec `AI_PROVIDER=none`. |
| SQLite dans un programme asynchrone | Requêtes minuscules et rapides : `sqlite3` standard suffit, sans dépendance supplémentaire. |

**Dépendances (versions épinglées et testées ensemble)** : `discord.py 2.7.1`
(commandes slash, boutons), `aiohttp 3.14.5` (déjà requis par discord.py, servira
aux webhooks en V2), `python-dotenv 1.2.4`, `pytest 9.1.1` et `pytest-asyncio 1.4.0`.
Python **3.12** est recommandé ; les tests passent aussi sous 3.11 et 3.13.

## 5. Plan de réalisation

| Étape | Contenu | Critère de validation |
|---|---|---|
| **V1** (livrée) | Bot Discord, `/health`, `/config`, `/setup` avec confirmation, `/refresh`, messages permanents idempotents, SQLite, tests, guide Windows | `/setup` reconnaît les 19 salons ; deux `/refresh` successifs ne créent aucun doublon |
| **V2** | Serveur de webhooks GitLab (push, Issues, MR, commentaires, pipelines), secret, idempotence, nouvelle table SQLite, redirection vers les salons, guide de configuration du webhook | Un « Test webhook » GitLab apparaît une seule fois dans le bon salon ; un mauvais secret est refusé |
| **V3** | Client API GitLab en lecture seule ; `/avancement`, `/bugs`, `/merge-requests`, `/bilan` fondés sur les données brutes ; bilan hebdomadaire planifié sans répétition au redémarrage | Les chiffres affichés correspondent à GitLab ; aucun bilan en double |
| **V4** | Interface `AIProvider` (fournisseur configurable, désactivable) ; `/resume-commit`, `/aide-projet`, synthèses ; journal IA volontaire avec export Markdown pour le SAE | Le bot fonctionne sans clé ; les réponses citent leurs liens et signalent les données manquantes |
| **V5** | Tests complets, durcissement sécurité, documentation finale, préparation à l'usage quotidien (service Windows ou hébergement) | Revue de sécurité et checklist d'exploitation |

Chaque étape s'appuie sur la précédente sans la réécrire : de nouveaux modules
(`src/gitlab/`, `src/ai/`), de nouvelles migrations SQLite ajoutées à la fin de la
liste et de nouveaux cogs.

## 6. Schéma de la base (V1)

| Table | Rôle |
|---|---|
| `managed_messages` | clé du message, salon, identifiant du message, empreinte du contenu, date |
| `channel_bindings` | fonction du bot → identifiant du salon (enregistré au `/setup` confirmé) |
| `kv_store` | état divers, par exemple la date du dernier `/setup` confirmé |

La version du schéma est stockée dans `PRAGMA user_version` ; les migrations sont
appliquées automatiquement au démarrage.
