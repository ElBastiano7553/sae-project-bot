# Sécurité : état des lieux (lot 4)

Synthèse des protections en place et des points qui dépendent de l'équipe ou du serveur.
Ce document décrit le code tel qu'il est, pas une certification.

## Secrets

| Secret | Où il vit | Protections |
|---|---|---|
| `DISCORD_BOT_TOKEN` | `.env` sur la machine du bot (`chmod 600`) | jamais affiché (`/admin config` dit seulement « défini ») ; masqué `***` dans tous les journaux ; absent de `repr(Settings)` |
| `GITLAB_WEBHOOK_SECRET` | `.env` | comparé à temps constant avant toute lecture de la requête ; masqué dans les journaux |
| `GITLAB_ACCESS_TOKEN` | `.env` | scope `read_api` uniquement ; envoyé seulement dans l'en-tête `PRIVATE-TOKEN`, en HTTPS (une adresse `http://` désactive l'API) ; redirections refusées |
| `AI_API_KEY` | `.env` | utilisée seulement par le SDK Anthropic ; jamais journalisée |

- `.env` est ignoré par Git (`.gitignore`) et exclu de l'image Docker (`.dockerignore`).
  La CI vérifie que l'image ne contient pas de `.env`.
- Avant tout envoi à l'IA, les secrets connus et ce qui ressemble à une clé sont masqués
  (voir ai.md §4).
- Aucun secret n'est écrit dans la base SQLite ni dans les sauvegardes.

## Discord

- Aucune permission Administrateur ni intent privilégié (vérifié par les tests).
- Mentions désactivées pour tous les messages du bot : `@everyone`, rôles et membres ne
  peuvent jamais être notifiés.
- Commandes d'administration : masquées par défaut aux membres sans « Gérer le serveur »,
  et droits **revérifiés par le bot** à chaque appel. Les actions sont journalisées
  (`/admin historique`).
- Actions destructives (reconstruction d'un message) : avertissement et confirmation
  explicite.
- Les réponses des commandes sont éphémères. Les erreurs affichées ne contiennent ni
  trace d'appel ni détail interne.

## Webhooks GitLab

- Le secret est vérifié en premier ; la taille est limitée à 2 Mo ; seul le JSON est
  accepté ; le projet peut être filtré (`GITLAB_PROJECT_ID`).
- Dédoublonnage par identifiant de livraison **et** par empreinte de l'événement.
- Textes GitLab nettoyés ; seules les URL `https://` sont rendues cliquables.
- En production : port ouvert uniquement sur `127.0.0.1`, exposé en HTTPS par un reverse
  proxy qui ne transmet que `/webhooks/gitlab` (deployment-linux.md §4).

## Conteneur

- Image `python:3.12.14-slim-bookworm`, version épinglée.
- Utilisateur non root (uid 10001), système de fichiers en lecture seule,
  `cap_drop: ALL`, `no-new-privileges`, limites de mémoire et de processus.
- Dépendances épinglées dans `requirements.txt`. `pip-audit` lancé le 22/10/2026 sur
  `requirements.txt` et `requirements-dev.txt` : aucune vulnérabilité connue à cette date.
  À relancer avant chaque mise en production : `pip install pip-audit && pip-audit -r requirements.txt`.

## Données

- Sauvegardes vérifiées (contrôle d'intégrité), automatiques, avant chaque migration et à
  la demande. La rotation ne touche que les fichiers créés par le bot.
- Les migrations sont transactionnelles et **n'ajoutent** que des tables, des colonnes et
  des index. Une base plus récente que le code est refusée.
- Conservation limitée (`HISTORY_RETENTION_DAYS`, 365 jours par défaut) ; le texte des
  questions posées à l'IA n'est jamais conservé.

## Points qui dépendent de l'équipe

- Garder `.env` hors de toute conversation, de tout dépôt et de toute capture d'écran.
- Donner « Gérer le serveur » (ou le rôle `ADMIN_ROLE_NAME`) seulement aux personnes qui
  administrent le bot.
- Régénérer immédiatement un token qui aurait fuité : portail développeur Discord, GitLab
  *Access tokens*, console Anthropic.
- Copier régulièrement les sauvegardes hors du serveur.
- Mettre à jour le serveur et l'image : `git pull` puis `docker compose up -d --build`
  reconstruit sur la dernière image de base épinglée.
