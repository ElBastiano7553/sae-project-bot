# Déployer les notifications GitHub (instructions, à exécuter plus tard)

Ces étapes **n'ont pas été exécutées**. Elles supposent que Samae tourne déjà sur le
VPS avec systemd (voir [deployment-vps.md](deployment-vps.md) ; avec Docker :
[deployment-linux.md](deployment-linux.md)) et que le reverse proxy
HTTPS des webhooks GitLab existe ou est prévu. Rien n'est exposé directement : le bot
écoute sur `127.0.0.1`, seul le reverse proxy (HTTPS) ou un tunnel le joint.

## 1. Mettre le code à jour

Après la fusion de la branche dans `main` (validée par Romain) :

```bash
bash deploy/update.sh      # VPS : met à jour depuis main et redémarre le service (deployment-vps.md §5)
```

Au premier démarrage, la base passe en **version 8** (création de la table
`github_deliveries` uniquement). Une sauvegarde vérifiée est faite automatiquement avant
la migration (`data/backups/…-avantv8.sqlite3`).

## 2. Ajouter les variables dans `.env` du serveur

```ini
GITHUB_WEBHOOK_SECRET=<secret généré, 32 caractères ou plus>
GITHUB_WEBHOOK_REPOSITORY=ElBastiano7553/game-project
GITHUB_WEBHOOK_HOST=127.0.0.1
GITHUB_WEBHOOK_PORT=8081
```

Ne collez jamais ce secret sur Discord, dans un ticket ou dans Git (`.env` est ignoré par Git).

### Si le bot tourne avec Docker Compose

Dans le conteneur, le serveur doit écouter sur toutes ses interfaces, et le port n'est
publié que sur `127.0.0.1` de l'hôte (comme 8080 pour GitLab). Ajoutez dans
`docker-compose.yml`, service du bot :

```yaml
    environment:
      GITHUB_WEBHOOK_HOST: 0.0.0.0
      GITHUB_WEBHOOK_PORT: "8081"
    ports:
      - "127.0.0.1:8081:8081"
```

(Ce fichier n'a pas été modifié : le VPS de l'équipe utilise systemd, pas Docker.)

### Si le bot tourne avec systemd (cas du VPS de l'équipe)

Gardez `GITHUB_WEBHOOK_HOST=127.0.0.1`. Aucun port à ouvrir dans le pare-feu : seul le
reverse proxy, sur la même machine, contacte `127.0.0.1:8081`.

## 3. Router `/webhooks/github` dans le reverse proxy

Exemple Caddy (`/etc/caddy/Caddyfile`), à côté du bloc GitLab existant :

```
bot.exemple-sae.fr {
    handle /webhooks/gitlab {
        reverse_proxy 127.0.0.1:8080
    }
    handle /webhooks/github {
        request_body {
            max_size 2MB
        }
        reverse_proxy 127.0.0.1:8081
    }
    handle {
        respond 404
    }
}
```

Puis `sudo caddy validate --config /etc/caddy/Caddyfile` et `sudo systemctl reload caddy`.
Seul le chemin `/webhooks/github` est transmis au port 8081.

Avec un Cloudflare Tunnel nommé, ajoutez une règle d'entrée équivalente
(`path: ^/webhooks/github$` → `http://127.0.0.1:8081`) avant la règle finale `http_status:404`.

## 4. Redémarrer et vérifier

```bash
sudo systemctl restart samae     # VPS (systemd) ; avec Docker : docker compose up -d
```

- `logs/bot.log` : « Webhooks GitHub : écoute sur http://… :8081/webhooks/github ».
- Sur le serveur : `curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8081/webhooks/github`
  doit afficher `401` (pas de signature : refus attendu).
- Depuis Internet : la même commande sur `https://bot.exemple-sae.fr/webhooks/github`
  doit aussi afficher `401`.
- `python -m src.github.demo ping` sur le serveur : `200 pong`.
- Sur Discord : `/status` → ligne GitHub ✅.

## 5. Créer le webhook GitHub

Suivez la section 5 de [github-webhooks.md](github-webhooks.md). Le `ping` initial doit
recevoir `200 pong` dans **Recent Deliveries**.

## Retour en arrière

Videz `GITHUB_WEBHOOK_SECRET` dans `.env` et redémarrez : le serveur GitHub ne démarre
plus, tout le reste fonctionne. Désactivez ensuite le webhook dans GitHub
(**Settings → Webhooks → Edit → Active** décoché). La table `github_deliveries` peut
rester en place : elle n'est lue par aucune autre commande.
