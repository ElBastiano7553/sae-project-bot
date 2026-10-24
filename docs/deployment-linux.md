# Déployer le bot sur un serveur Linux (La Banquise) avec Docker

Ce guide prépare un déploiement durable : le bot tourne en continu dans un conteneur
Docker, redémarre seul et sauvegarde sa base. **Rien n'est déployé automatiquement** :
chaque étape se fait à la main, sur le serveur.

> Ce dépôt ne contient aucune information sur la configuration réelle du serveur de
> La Banquise. Les points marqués **« à vérifier »** dépendent de ce qu'elle fournit :
> système, accès administrateur, Docker, nom de domaine, ports ouverts.

## 0. Prérequis (à vérifier avec La Banquise)

| Besoin | Pourquoi | Si ce n'est pas possible |
|---|---|---|
| Un serveur Linux 64 bits (x86_64) avec un accès SSH | exécuter le bot en continu | — |
| Docker Engine et le plugin `docker compose` (ou le droit de les installer) | lancer l'image | installation sans Docker, §8 |
| Sortie HTTPS vers `discord.com`, `gateway.discord.gg`, `gitlab.com` (ou votre GitLab), `api.anthropic.com` (si l'IA est activée) | Discord, API GitLab, IA | le bot ne peut pas fonctionner |
| **Pour les webhooks GitLab seulement** : un nom de domaine qui pointe vers le serveur avec les ports 80/443 ouverts, **ou** un compte Cloudflare avec un domaine (tunnel nommé) | GitLab doit joindre le bot en HTTPS | sans webhooks, tout le reste fonctionne (V1, commandes, IA sauf `/ia bilan` qui n'aurait pas de données) |
| Environ 1 Go d'espace disque et 512 Mo de mémoire | image (~290 Mo), base, sauvegardes, journaux | — |

En production, n'utilisez **pas** ngrok ni un *Quick Tunnel* Cloudflare : leur adresse
change à chaque redémarrage, et ce ne sont pas des services conçus pour un usage continu.

## 1. Récupérer le code

```bash
sudo mkdir -p /opt/sae-project-bot && sudo chown "$USER": /opt/sae-project-bot
git clone https://github.com/ElBastiano7553/sae-project-bot.git /opt/sae-project-bot
cd /opt/sae-project-bot
git checkout main        # la version validée par l'équipe
```

## 2. Créer le fichier `.env` sur le serveur

```bash
cp .env.example .env
chmod 600 .env           # lisible par vous seul
nano .env                # remplissez DISCORD_BOT_TOKEN, DISCORD_GUILD_ID, etc.
```

- Tapez ou collez les secrets **directement dans le terminal du serveur**. Ne les envoyez
  jamais par Discord, par e-mail ou dans un ticket.
- Ne modifiez pas `WEBHOOK_HOST`, `WEBHOOK_PORT` ni `DATABASE_PATH` : `docker-compose.yml`
  fixe ces valeurs pour le conteneur.
- Pour reprendre la base d'un autre ordinateur, copiez son fichier `data/sae_bot.sqlite3`
  dans `data/` **avant** le premier démarrage. Le bot étant arrêté, une copie avec `scp`
  convient.

## 3. Préparer les dossiers et lancer

Le conteneur tourne avec l'utilisateur non privilégié `10001`. Il doit pouvoir écrire
dans `data/` et `logs/` :

```bash
mkdir -p data logs
sudo chown -R 10001:10001 data logs
docker compose up -d --build
docker compose ps        # « healthy » après une à deux minutes
docker compose logs -f   # Ctrl+C pour quitter l'affichage, le bot continue
```

Sur Discord, `/etat` doit afficher la connexion, la base (schéma v7), les salons
reconnus et les sauvegardes. Si c'est la première installation, lancez `/admin installer`.

### Ce que fait `docker-compose.yml`

- **Redémarrage** : redémarre automatiquement le bot (`restart: unless-stopped`), aussi
  après un redémarrage du serveur si le service Docker démarre au boot. À vérifier :
  `sudo systemctl is-enabled docker`.
- **Contrôle de santé** : vérifie chaque minute, avec `python -m src.healthcheck`, que le
  bot est connecté à Discord.
- **Système de fichiers en lecture seule**. Seuls `data/` et `logs/` sont accessibles en
  écriture. `config/`, `content/` et `knowledge/` sont montés en lecture seule : on les
  modifie sur le serveur, puis `/admin actualiser` (ou un redémarrage pour `config/gitlab.toml`).
- **Aucun privilège** : `cap_drop: ALL`, `no-new-privileges`, utilisateur non root.
- **Limites de ressources** : 512 Mo de mémoire au plus, 128 processus au plus.
- **Port des webhooks** : ouvert uniquement sur `127.0.0.1:8080`, donc invisible depuis
  Internet.
- **Journaux** : `logs/bot.log` (rotation 3 × 1 Mo) et `docker compose logs`
  (rotation 3 × 10 Mo).

## 4. Webhooks GitLab en HTTPS (facultatif)

Le bot n'accepte que les requêtes portant le bon secret. Il doit cependant rester derrière
HTTPS : le secret circule dans un en-tête.

### Option A : reverse proxy Caddy (domaine pointant vers le serveur, ports 80/443 ouverts)

Caddy obtient et renouvelle seul le certificat HTTPS. Exemple de `/etc/caddy/Caddyfile`,
à adapter avec votre domaine :

```
bot.exemple-sae.fr {
    handle /webhooks/gitlab {
        reverse_proxy 127.0.0.1:8080
    }
    handle {
        respond 404
    }
}
```

`sudo systemctl reload caddy`. L'URL du webhook GitLab devient alors
`https://bot.exemple-sae.fr/webhooks/gitlab`. Seul ce chemin est transmis au bot.

### Option B : Cloudflare Tunnel nommé (aucun port ouvert)

Suivez l'option C de [gitlab-webhooks.md](gitlab-webhooks.md), puis installez
`cloudflared` comme service : `sudo cloudflared service install`. L'adresse reste fixe.

Dans les deux cas, terminez par le test « Test > Push events » de GitLab (voir
gitlab-webhooks.md §6). `/etat` doit indiquer « webhooks actifs ».

## 5. Sauvegardes

- **Automatiques** : une sauvegarde vérifiée toutes les `BACKUP_INTERVAL_HOURS` (24 h par
  défaut) dans `data/backups/`. Les `BACKUP_KEEP` plus récentes sont conservées (14 par
  défaut).
- **Avant chaque migration** du schéma, une copie `…-avantvN.sqlite3` est créée. Si elle
  échoue (disque plein…), le bot refuse de démarrer **sans toucher à la base**.
- **À la demande** : `/admin sauvegarde` sur Discord.
- **Hors du serveur (recommandé)** : les sauvegardes sont sur le même disque que la base.
  Copiez-les régulièrement ailleurs, par exemple depuis votre ordinateur :
  `scp -r utilisateur@serveur:/opt/sae-project-bot/data/backups ./sauvegardes-sae`

### Restaurer une sauvegarde

À faire seulement si la base est endommagée ou après une erreur. Rien n'est supprimé :
la base actuelle est mise de côté.

```bash
cd /opt/sae-project-bot
docker compose stop
ls -lt data/backups/                      # choisir la sauvegarde
mkdir -p data/avant-restauration
sudo mv data/sae_bot.sqlite3* data/avant-restauration/   # base actuelle + fichiers -wal/-shm éventuels
sudo cp data/backups/sae_bot-AAAAMMJJ-HHMMSS.sqlite3 data/sae_bot.sqlite3
sudo chown 10001:10001 data/sae_bot.sqlite3
docker compose start
```

Vérifiez ensuite `/etat` et `/admin messages`. Les événements GitLab reçus après la
date de la sauvegarde ne sont plus dans l'historique.

## 6. Mettre à jour

```bash
cd /opt/sae-project-bot
git pull                       # sur main, après validation de la PR
docker compose up -d --build   # reconstruit et relance ; migration (avec sauvegarde) si besoin
docker compose ps
```

Revenir à la version précédente : `git checkout <commit précédent>` puis
`docker compose up -d --build`. Une base déjà migrée vers un schéma plus récent est
refusée par une ancienne version. Dans ce cas, restaurez la sauvegarde `…-avantvN`
créée juste avant la migration (§5).

## 7. Surveiller

| Commande | Pour |
|---|---|
| `docker compose ps` | état et santé (`healthy` / `unhealthy`) |
| `docker compose logs --tail 100` | derniers journaux |
| `docker compose exec bot python -m src.healthcheck` | contrôle de santé à la main |
| `/etat` sur Discord | état complet, dernière erreur, sauvegardes, IA |
| `/activite evenements type:Erreurs du bot` (admins) | erreurs regroupées |

`ALERTS_CHANNEL` permet d'être prévenu sur Discord en cas d'erreurs répétées (voir
history-and-stats.md §5). Si le bot est complètement arrêté, il ne peut pas prévenir :
seule une surveillance externe le détecterait (à voir avec La Banquise).

## 8. Sans Docker (service systemd)

> **Installation actuelle de l'équipe (VPS Debian, projet dans le dossier personnel) :
> suivez [deployment-vps.md](deployment-vps.md).** Ses scripts (`deploy/`) installent le
> service, les sauvegardes avant mise à jour, le retour arrière et la surveillance.
> L'exemple ci-dessous vaut seulement pour un projet installé dans `/opt` : avec
> `ProtectHome=true`, un projet placé dans `/home` serait invisible pour le service.

Si Docker n'est pas disponible, vous pouvez lancer le bot comme service système. Il faut
Python 3.11 ou plus récent.

```bash
sudo useradd --system --home /opt/sae-project-bot --shell /usr/sbin/nologin saebot
cd /opt/sae-project-bot
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
sudo chown -R saebot: data logs && sudo chown saebot: .env && sudo chmod 600 .env
```

Fichier `/etc/systemd/system/sae-project-bot.service` :

```ini
[Unit]
Description=SAE Project Bot
After=network-online.target
Wants=network-online.target

[Service]
User=saebot
WorkingDirectory=/opt/sae-project-bot
ExecStart=/opt/sae-project-bot/.venv/bin/python -m src.main
Restart=on-failure
RestartSec=10
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/opt/sae-project-bot/data /opt/sae-project-bot/logs
ProtectHome=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

`sudo systemctl daemon-reload && sudo systemctl enable --now sae-project-bot`, puis
`journalctl -u sae-project-bot -f`. Le bot écoute alors directement sur
`127.0.0.1:8080` (valeur par défaut de `WEBHOOK_HOST`). Le reverse proxy du §4 reste
valable.
