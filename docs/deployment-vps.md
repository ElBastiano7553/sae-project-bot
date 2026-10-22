# Héberger Samae sur un VPS Debian avec systemd

Ce guide concerne l'installation actuelle : un VPS Debian 12, le dépôt cloné dans le
dossier personnel de l'utilisateur du bot (`~/sae-project-bot`), un environnement virtuel
`.venv` et un `.env` déjà rempli. Il remplace le lancement à la main
(`python -m src.main` dans une session SSH) par un **service systemd** : le bot tourne en
continu, sans votre ordinateur ni session SSH ouverte.

> Ce document est public : il ne contient ni l'adresse du serveur, ni le nom
> d'utilisateur SSH, ni aucun secret. Remplacez `utilisateur@serveur` par vos valeurs.

## 1. Pourquoi systemd plutôt que Docker

| Critère | systemd (choisi) | Docker (docs/deployment-linux.md) |
|---|---|---|
| Installation existante | réutilisée telle quelle (`.venv`, `.env`, `data/`) | à reconstruire, données à déplacer, droits à changer (utilisateur 10001) |
| À installer en plus | rien (systemd est déjà là) | Docker Engine (~400 Mo), une image à reconstruire à chaque mise à jour |
| Redémarrage auto (crash, boot) | oui (`Restart=on-failure`, `enable`) | oui (`restart: unless-stopped`) |
| Bot bloqué sans crash | relancé par le chien de garde (`WatchdogSec`) | signalé `unhealthy`, **pas** relancé par Docker seul |
| Isolation | forte (voir §6 ; `systemd-analyze security` : 2,1 « OK ») | forte |
| Complexité pour l'équipe | 3 scripts, commandes `systemctl` / `journalctl` | commandes `docker compose`, image, volumes |

Docker n'apporte pas de bénéfice réel ici : un seul processus Python, déjà installé et
testé. Le `Dockerfile` reste dans le dépôt (CI, autre hébergeur) mais n'est pas utilisé
sur le VPS.

## 2. Ce qui est installé

| Fichier sur le serveur | Rôle |
|---|---|
| `/etc/systemd/system/samae.service` | le bot (généré depuis `deploy/samae.service.in`) |
| `/etc/systemd/system/samae-check.service` + `.timer` | contrôle toutes les 5 minutes (bot, mémoire, disque, sauvegardes) |
| `/etc/systemd/journald.conf.d/samae.conf` | journaux système conservés après un redémarrage, 300 Mo au plus |

Les scripts du dossier `deploy/` font tout le travail :

| Script | Quand |
|---|---|
| `bash deploy/install-service.sh` | une fois (puis après un changement de `DATABASE_PATH` ou `BACKUP_DIR`) |
| `bash deploy/update.sh` | à chaque nouvelle version validée sur `main` |
| `bash deploy/rollback.sh` | si une mise à jour pose problème |

Ils se lancent avec l'utilisateur du bot (**pas root**) et demandent `sudo` quand il faut.
Ils ne lisent dans `.env` que `DATABASE_PATH` et `BACKUP_DIR` : aucun secret n'est lu ni
affiché. Ils ne font jamais de `git reset`, de `git clean` ni de suppression : s'ils
trouvent des fichiers du dépôt modifiés sur le serveur, ils s'arrêtent sans rien changer.

## 3. Première installation (passage du lancement manuel au service)

À faire une seule fois, dans un terminal SSH. Comptez 10 minutes.

```bash
ssh utilisateur@serveur
cd ~/sae-project-bot
```

**1. Arrêter le bot lancé à la main.** S'il tourne dans un terminal, `Ctrl+C` dans ce
terminal (arrêt propre). Sinon :

```bash
pgrep -af "src.main"          # affiche le numéro du processus s'il tourne
kill <numéro>                 # arrêt propre (SIGTERM)
```

**2. Noter la version actuelle** (pour pouvoir y revenir) et vérifier que rien n'a été
modifié à la main :

```bash
git status                    # doit dire « nothing to commit, working tree clean »
git log -1 --oneline          # notez ce numéro de commit
git rev-parse HEAD > ~/version-avant-service.txt
```

Si `git status` liste des fichiers modifiés, **arrêtez-vous** : `git diff` montre les
changements. Ne lancez ni `git reset` ni `git checkout -- .` ; demandez de l'aide.

**3. Récupérer la nouvelle version** (le code seulement : `.env`, `data/` et `logs/` ne
sont pas suivis par Git et ne sont pas touchés) :

```bash
git checkout main
git pull --ff-only
.venv/bin/pip install -r requirements.txt
```

**4. Sauvegarder la base et vérifier que la sauvegarde est restaurable** (avant que la
nouvelle version n'ouvre la base) :

```bash
.venv/bin/python -m src.ops backup
.venv/bin/python -m src.ops restore-test
```

Les deux lignes doivent commencer par ✅. La sauvegarde est dans
`data/backups/deploiements/`.

**5. Éviter toute modification du serveur Discord au premier démarrage.** La version
1.2 a changé le texte des messages permanents. Par défaut, le bot les met à jour au
démarrage. Pour garder la main, mettez temporairement dans `.env` :

```bash
nano .env        # AUTO_REFRESH_ON_STARTUP=false , puis Ctrl+O, Entrée, Ctrl+X
```

**6. Installer et démarrer le service :**

```bash
bash deploy/install-service.sh
```

Le script vérifie `.venv`, `.env` (et le remet en `chmod 600`), les dossiers
d'écriture, l'absence d'un autre bot, installe le service, l'active au démarrage du
serveur, le lance, puis attend la connexion à Discord. Il se termine par
« Samae tourne en service ».

**7. Vérifier sur Discord** (voir aussi le plan de test, docs/test-manuel.md) :
`/status` (tout en ✅ ou ⏸️, ligne « Réactivité »), `/help`, puis `/setup` **sans
confirmer** pour voir ce que la mise à jour des messages changerait. Quand le résultat
vous convient : `/refresh` (modifie les messages sur place, ne supprime rien), puis
remettez `AUTO_REFRESH_ON_STARTUP=true` dans `.env` et `sudo systemctl restart samae`.

**8. Tester le redémarrage automatique** (facultatif, recommandé) :

```bash
sudo reboot                   # la session SSH se ferme ; attendez 2 minutes
ssh utilisateur@serveur
systemctl status samae        # « active (running) »
```

Ne lancez **pas** `/refonte` sur le vrai serveur sans l'avoir testée sur un serveur de
test (docs/test-manuel.md).

### Sans sudo

Si `sudo -v` échoue, l'utilisateur du bot n'a pas les droits d'administration. Le plus
simple est de les lui donner une fois, depuis le compte root (`su -`) :
`usermod -aG sudo <utilisateur du bot>`, puis déconnexion et reconnexion SSH. Le service,
lui, ne tourne jamais avec ces droits (`NoNewPrivileges`).

## 4. Utilisation au quotidien

| Je veux… | Commande |
|---|---|
| savoir si le bot tourne | `systemctl status samae` |
| un contrôle complet (connexion, mémoire, disque, sauvegardes) | `cd ~/sae-project-bot && .venv/bin/python -m src.ops check` |
| voir les journaux en direct | `sudo journalctl -u samae -f` (Ctrl+C pour quitter, le bot continue) |
| voir les 100 dernières lignes | `sudo journalctl -u samae -n 100 --no-pager` |
| les journaux d'hier | `sudo journalctl -u samae --since yesterday --until today` |
| seulement les erreurs | `sudo journalctl -u samae -p err --since "-24h"` |
| redémarrer | `sudo systemctl restart samae` |
| arrêter / démarrer | `sudo systemctl stop samae` / `sudo systemctl start samae` |
| savoir combien de fois il a redémarré | `systemctl show samae -p NRestarts` |
| voir les derniers contrôles | `sudo journalctl -u samae-check -n 20 --no-pager` |
| mémoire et CPU du bot | `systemctl status samae` (lignes `Memory` et `CPU`) |

Sur Discord, `/status` donne aussi l'état mesuré à l'instant (connexion, base,
sauvegardes, réactivité des commandes, dernière erreur).

Le bot écrit aussi `logs/bot.log` (3 × 1 Mo au plus, rotation automatique). Les journaux
systemd sont limités à 300 Mo et un mois. Aucun token ni secret n'y apparaît : ils sont
masqués avant écriture.

> Ne lancez plus `python -m src.main` à la main sur le serveur pendant que le service
> tourne : la deuxième instance refuse de démarrer (« une autre instance du bot tourne
> déjà »), ce qui évite les réponses en double. Pour tester à la main :
> `sudo systemctl stop samae` d'abord.

## 5. Mettre à jour

Après la fusion d'une PR dans `main` (jamais avant) :

```bash
cd ~/sae-project-bot
bash deploy/update.sh
```

Le script affiche les changements et demande confirmation, puis :

1. vérifie que le dépôt n'a pas été modifié sur le serveur ;
2. fait une **sauvegarde vérifiée** de la base et un **test de restauration** ; si l'un
   échoue, il s'arrête sans rien changer ;
3. note la version actuelle dans `data/version-precedente` et `data/deploiements.log` ;
4. arrête le bot, met le code à jour (`git merge --ff-only`, jamais de réécriture),
   installe les dépendances ;
5. redémarre et attend la connexion à Discord (150 s au plus) ;
6. en cas d'échec, affiche les journaux et propose le retour à la version précédente.

Ensuite : `/status` et `/help` sur Discord.

## 6. Revenir en arrière

```bash
cd ~/sae-project-bot
bash deploy/rollback.sh              # version d'avant la dernière mise à jour
bash deploy/rollback.sh 2eaf73c      # ou un commit précis (git log --oneline)
```

Le script arrête le bot, remet le code de cette version (`git checkout`, HEAD détaché :
c'est normal), réinstalle ses dépendances et vérifie la base :

- si son schéma est compatible (cas des versions 1.1 → 1.2, toutes en schéma v7), la
  base est gardée telle quelle : **aucune donnée perdue** ;
- si la nouvelle version avait migré la base vers un schéma plus récent, il propose de
  restaurer la sauvegarde faite juste avant la mise à jour (la base actuelle est
  déplacée dans `data/avant-restauration-…/`, pas effacée). Les changements faits depuis
  la mise à jour ne seront alors plus dans la base.

La mise à jour suivante (`bash deploy/update.sh`) revient automatiquement sur `main`.

## 7. Sauvegardes

| Sauvegarde | Où | Combien |
|---|---|---|
| Automatique, toutes les `BACKUP_INTERVAL_HOURS` (24 h) | `data/backups/` | `BACKUP_KEEP` (14) |
| Avant chaque mise à jour (`update.sh`) ou `python -m src.ops backup` | `data/backups/deploiements/` | 10 |
| Avant une migration du schéma | `data/backups/` (`…-avantvN`) | 30 |
| Avant une `/refonte` | `data/backups/` (`…-avantrefonte`) | avec les automatiques |
| À la demande sur Discord | `/admin backup` | avec les automatiques |

Chaque copie est faite avec l'API de sauvegarde de SQLite (cohérente même si le bot
écrit) puis rouverte et vérifiée (`PRAGMA integrity_check`).

| Commande (`cd ~/sae-project-bot` d'abord) | Effet |
|---|---|
| `.venv/bin/python -m src.ops verify` | vérifie la sauvegarde la plus récente (ou un fichier donné) |
| `.venv/bin/python -m src.ops restore-test` | **prouve** qu'elle est restaurable : copie temporaire ouverte par le code actuel, vraie base intacte |

Faites un `restore-test` de temps en temps (par exemple une fois par mois).

**Copie hors du serveur (fortement conseillé).** Toutes ces sauvegardes sont sur le
disque du VPS. Depuis votre ordinateur (PowerShell sous Windows) :

```bash
scp -r utilisateur@serveur:sae-project-bot/data/backups ./sauvegardes-samae
```

### Restaurer une sauvegarde

Seulement si la base est endommagée ou après une erreur grave :

```bash
cd ~/sae-project-bot
sudo systemctl stop samae
.venv/bin/python -m src.ops restore data/backups/sae_bot-AAAAMMJJ-HHMMSS.sqlite3            # simulation
.venv/bin/python -m src.ops restore data/backups/sae_bot-AAAAMMJJ-HHMMSS.sqlite3 --confirm  # pour de vrai
sudo systemctl start samae
```

La restauration est refusée tant que le bot tourne. La base actuelle est déplacée dans
`data/avant-restauration-<date>/`, jamais supprimée.

## 8. Surveillance et alertes

`samae-check.timer` lance `python -m src.ops check --alert` toutes les 5 minutes :

| Contrôle | Problème si |
|---|---|
| fichier de vie du bot | plus vieux que 3 minutes, ou bot non connecté à Discord |
| mémoire du bot | plus de 400 Mo (limite dure du service : 512 Mo) |
| disque | plus de 90 % utilisé ou moins de 500 Mo libres |
| sauvegardes automatiques | aucune, ou la dernière a plus de 2 intervalles |

Le résultat est dans `sudo journalctl -u samae-check`. Pour être **prévenu sur Discord**,
même quand le bot est arrêté (l'alerte ne passe pas par le bot) :

1. sur Discord, dans un salon réservé aux administrateurs : Paramètres du salon >
   Intégrations > Webhooks > Nouveau webhook > Copier l'URL ;
2. sur le serveur : `nano .env` et ajoutez `ALERT_WEBHOOK_URL=<l'URL copiée>` ;
3. aucun redémarrage nécessaire : le prochain contrôle l'utilise.

L'URL d'un webhook permet d'écrire dans le salon : traitez-la comme un mot de passe. Une
alerte est envoyée quand un problème **apparaît**, un rappel toutes les 12 heures s'il
dure, et un message quand **tout est revenu à la normale** : pas de message toutes les
5 minutes. Les erreurs répétées du bot lui-même restent signalées par `ALERTS_CHANNEL`
(voir history-and-stats.md).

## 9. Sécurité de l'installation

Ce que fait le service (`deploy/samae.service.in`) :

- tourne avec l'utilisateur du bot, **jamais root**, sans aucune capacité ni moyen
  d'en obtenir (`NoNewPrivileges`, `CapabilityBoundingSet=` vide) ;
- tout le système est en **lecture seule** pour le bot, y compris le code et `.env` ;
  il ne peut écrire que dans le dossier de la base (`data/`), le dossier des sauvegardes
  et `logs/`. Le projet étant dans `/home`, `ProtectHome=read-only` est utilisé (avec
  `ProtectHome=true`, le bot ne verrait plus son propre dossier) ;
- `/tmp` privé, pas d'accès aux périphériques, au noyau ni aux autres utilisateurs ;
  appels système limités à ceux d'un service ordinaire ;
- mémoire limitée à 512 Mo, 128 processus légers au plus ;
- fichiers créés lisibles par l'utilisateur du bot seulement (`UMask=0077`).

À vérifier vous-même de temps en temps :

```bash
ls -l .env                     # -rw------- : lisible par vous seul
sudo ss -tlnp                  # ports en écoute
```

Le bot n'ouvre **aucun port** tant que les webhooks GitLab et GitHub sont désactivés
(`GITLAB_WEBHOOK_SECRET` et `GITHUB_WEBHOOK_SECRET` vides). S'ils sont activés, il écoute
sur `127.0.0.1:8080` (GitLab) et `127.0.0.1:8081` (GitHub) seulement, invisibles depuis
Internet ; un reverse proxy HTTPS ou un tunnel transmet les webhooks
(docs/deployment-linux.md §4 pour GitLab ; pour GitHub, route `/webhooks/github` →
`127.0.0.1:8081`, voir docs/github-deploiement.md §3). Aucun de ces ports ne doit être
ouvert dans le pare-feu. Dans `ss -tlnp`, seul `sshd` (port 22) doit être
ouvert sur `0.0.0.0` ou `*`. Recommandé sur le VPS : mises à jour de sécurité
automatiques (`sudo apt install unattended-upgrades`) et connexion SSH par clé.

## 10. Dépannage

| Symptôme | Cause probable | Que faire |
|---|---|---|
| `status=2` dans `systemctl status samae`, le service ne redémarre pas | configuration à corriger (`.env`, `config/`, token refusé) | `sudo journalctl -u samae -n 30`, corrigez, puis `sudo systemctl restart samae` |
| `status=3` | un bot tourne déjà en dehors du service | `pgrep -af src.main`, arrêtez-le, puis `sudo systemctl restart samae` |
| `start-limit-hit` | 5 échecs en 15 minutes | lisez les journaux, corrigez, `sudo systemctl reset-failed samae && sudo systemctl start samae` |
| `status=226/NAMESPACE` | un dossier d'écriture n'existe plus | relancez `bash deploy/install-service.sh` |
| `Watchdog timeout` dans les journaux | le bot était bloqué : systemd l'a relancé | signalez-le avec l'heure ; les journaux juste avant indiquent l'opération en cours |
| « Sauvegarde automatique échouée … Read-only file system » | `BACKUP_DIR` modifié dans `.env` après l'installation | relancez `bash deploy/install-service.sh` |
| les commandes slash n'apparaissent pas | synchronisation ignorée ou refusée | `.venv/bin/python -m src.ops resync`, puis `sudo systemctl restart samae` |
| `/status` affiche « Réactivité » en ⚠️ | commandes lentes ou en erreur | `sudo journalctl -u samae -g "lente\|Erreur pendant"` |

Les commandes slash ne sont envoyées à Discord que lorsqu'elles ont changé (empreinte
enregistrée en base) : un redémarrage ordinaire ne les resynchronise pas, ce qui rend le
démarrage plus rapide et évite les limites de Discord.
