# Checklist du propriétaire du VPS : installer Lavalink pour la musique de Samae

À faire **une fois**, par la personne qui a les droits d'administration (`sudo`) sur le VPS
Debian 12, **après** la fusion de la version 1.11.0 dans `main` et le test manuel sur un
serveur Discord de test ([music.md](music.md), §8). Comptez 20 minutes.

Rien ici ne touche au service `samae`, à son `.env` ni à sa base : Lavalink est un service
**séparé**, sous son **propre compte**, qui n'écoute que sur la machine elle-même
(127.0.0.1:2333, jamais exposé sur Internet, aucun port à ouvrir dans le pare-feu).

> Document public : aucune adresse ni secret. Le mot de passe est généré à l'étape 5 et
> transmis à l'utilisateur du bot par un canal privé, jamais dans Discord ni dans Git.

Légende : 🔑 = demande `sudo` (propriétaire) · 👤 = faisable par l'utilisateur du bot, sans sudo.

## 0. Vérifier la place 👤

```bash
free -m          # il faut ~800 Mo de mémoire « available » en plus de l'usage actuel
df -h /opt       # ~100 Mo libres suffisent (jar + plugin + journaux plafonnés à 200 Mo)
```

Si la mémoire manque, ne pas continuer : la musique reste désactivée, le bot n'est pas affecté.

## 1. Installer Java 17 🔑

```bash
sudo apt update
sudo apt install openjdk-17-jre-headless
java -version      # « openjdk version "17… »
```

(Paquet officiel de Debian 12, gratuit. Rien d'autre à installer.)

## 2. Créer le compte et les dossiers 🔑

```bash
sudo adduser --system --group --home /opt/lavalink --no-create-home --shell /usr/sbin/nologin lavalink
sudo install -d -o root -g lavalink -m 0750 /opt/lavalink /opt/lavalink/plugins
sudo install -d -o lavalink -g lavalink -m 0750 /opt/lavalink/logs
sudo install -d -o root -g lavalink -m 0750 /etc/lavalink
```

Le compte `lavalink` n'a ni mot de passe, ni shell, ni sudo. Il ne peut écrire que dans
`/opt/lavalink/logs`.

## 3. Télécharger Lavalink et le plugin YouTube, vérifier les empreintes 🔑

```bash
cd /tmp
curl -fLO https://github.com/lavalink-devs/Lavalink/releases/download/4.2.2/Lavalink.jar
curl -fLO https://github.com/lavalink-devs/youtube-source/releases/download/1.18.2/youtube-plugin-1.18.2.jar
sha256sum Lavalink.jar youtube-plugin-1.18.2.jar
```

Les empreintes doivent être **exactement** :

```
8cb801e591072c3689fafd71ccf571a95a4ead3cc35dfc045e157d763d89119a  Lavalink.jar
dd4b3bce50dbc7582c80776bad47bad87bef1dc683c69d66ab4a084aa3006238  youtube-plugin-1.18.2.jar
```

Si une seule diffère : **s'arrêter**, supprimer les fichiers téléchargés et prévenir.
Sinon :

```bash
sudo install -o root -g lavalink -m 0640 Lavalink.jar /opt/lavalink/Lavalink.jar
sudo install -o root -g lavalink -m 0640 youtube-plugin-1.18.2.jar /opt/lavalink/plugins/
rm Lavalink.jar youtube-plugin-1.18.2.jar
```

## 4. Configuration de Lavalink 🔑

Depuis le dépôt du bot (déjà à jour en 1.12.0) :

```bash
sudo install -o root -g lavalink -m 0640 ~utilisateur-du-bot/sae-project-bot/deploy/lavalink/application.yml.example /opt/lavalink/application.yml
```

Le relire (`sudo less /opt/lavalink/application.yml`) : écoute `127.0.0.1`, sources `http` et
`local` à `false`, aucun mot de passe écrit dedans, aucun téléchargement automatique de plugin.

## 5. Mot de passe 🔑

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"     # copier la valeur affichée
sudo install -o root -g lavalink -m 0640 /dev/null /etc/lavalink/lavalink.env
sudo nano /etc/lavalink/lavalink.env
```

Contenu (une ligne, la valeur générée à la place de `…`) :

```
LAVALINK_SERVER_PASSWORD=…
```

Vérifier les droits : `sudo ls -l /etc/lavalink/lavalink.env` → `-rw-r----- root lavalink`.
Transmettre la valeur à l'utilisateur du bot **en privé** (étape 9).

## 6. Service systemd 🔑

```bash
sudo install -o root -g root -m 0644 ~utilisateur-du-bot/sae-project-bot/deploy/lavalink/lavalink.service.example /etc/systemd/system/lavalink.service
sudo systemd-analyze verify /etc/systemd/system/lavalink.service   # aucune erreur attendue
sudo systemctl daemon-reload
sudo systemctl enable --now lavalink
```

Le service a été écrit avec des protections fortes (système en lecture seule, pas de
privilèges, mémoire plafonnée à 768 Mo). **Il n'a pas pu être essayé sous systemd** dans
l'environnement de développement (Lavalink lui-même, oui) : bien faire l'étape 7.

## 7. Contrôles 🔑 (sauf mention)

```bash
systemctl status lavalink                         # « active (running) »
sudo journalctl -u lavalink -n 50 --no-pager      # chercher « Lavalink is ready to accept connections »
                                                  # et « Loaded 'youtube-plugin-1.18.2.jar' »
sudo ss -ltnp | grep 2333                         # doit montrer 127.0.0.1:2333, JAMAIS 0.0.0.0 ni *
```

👤 Sans sudo, l'utilisateur du bot peut vérifier que Lavalink répond et refuse sans mot de passe :

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:2333/v4/info    # 401 attendu
```

(Avec le mot de passe : `curl -s -H "Authorization: $MDP" http://127.0.0.1:2333/v4/info`
où `MDP` est saisi avec `read -rs MDP` pour qu'il ne reste pas dans l'historique.)

Si le service redémarre en boucle : `sudo journalctl -u lavalink -n 100 --no-pager` ; causes
habituelles : Java absent ou ancien, `application.yml` illisible par le groupe `lavalink`,
mémoire insuffisante (`MemoryMax`), dossier `logs` non inscriptible.

## 8. Mémoire réelle 🔑

```bash
systemctl status lavalink | grep -i memory       # ~300 Mo attendus au repos
```

À refaire pendant le test d'écoute (étape 10) pour noter la consommation en lecture.

## 9. Activer la musique dans le bot 👤 (utilisateur du bot)

1. `cd ~/sae-project-bot && bash deploy/update.sh` : met à jour en 1.12.0, fait la
   **sauvegarde vérifiée** de la base puis la migration v13 au démarrage (création de
   3 tables, aucune donnée existante modifiée).
2. Dans `.env` (éditeur de texte, jamais copié dans un message) :
   ```
   MUSIC_ENABLED=true
   LAVALINK_PASSWORD=<la valeur de l'étape 5>
   ```
3. Redémarrer le bot : `sudo systemctl restart samae` 🔑 (ou le demander au propriétaire).
4. Sur Discord : `/etat` → ligne « Musique : prête ».

## 10. Premier essai 👤

Dans un salon vocal du serveur : `/play query:<un titre>` et vérifier que **le son
s'entend**. Puis `/audiocheck` : les 5 étapes doivent être ✅ (sinon, la cause probable est
affichée : voir [music.md](music.md), §8 bis). Si YouTube refuse (journal Lavalink : « Sign in to confirm you're not a bot » ou
« This video is unavailable »), mettre `MUSIC_SOURCES=soundcloud,bandcamp` dans `.env` et
redémarrer le bot. **Ne pas** ajouter de compte Google, de cookies ni de jeton « poToken ».

## Mises à jour de Lavalink ou du plugin 🔑

Uniquement vers une version annoncée dans une PR du dépôt (avec sa nouvelle empreinte) :

```bash
sudo cp -a /opt/lavalink/Lavalink.jar /opt/lavalink/Lavalink.jar.precedent
sudo cp -a /opt/lavalink/plugins /opt/lavalink/plugins.precedent
# télécharger, vérifier sha256sum, installer comme à l'étape 3 (retirer l'ancien plugin de plugins/)
sudo systemctl restart lavalink && sudo journalctl -u lavalink -n 50 --no-pager
```

Le bot se reconnecte seul ~10 s après le retour de Lavalink ; les files en cours sont vidées.

## Revenir en arrière 🔑

```bash
sudo systemctl stop lavalink
sudo cp -a /opt/lavalink/Lavalink.jar.precedent /opt/lavalink/Lavalink.jar
sudo rm -r /opt/lavalink/plugins && sudo cp -a /opt/lavalink/plugins.precedent /opt/lavalink/plugins
sudo systemctl start lavalink
```

## Tout désactiver (sans rien supprimer)

- 👤 `.env` : `MUSIC_ENABLED=false`, puis redémarrage du bot 🔑 : les commandes de musique (`/play`…)
  expliquent que la musique est désactivée ; historique et favoris restent en base.
- 🔑 `sudo systemctl disable --now lavalink` : Lavalink ne tourne plus et ne redémarre plus.

## Récapitulatif sudo / sans sudo

| Étape | Qui |
|---|---|
| 0 vérifier la place, 7 test `curl` sans mot de passe, 9.1 `update.sh`, 9.2 `.env`, 10 essai | 👤 utilisateur du bot |
| 1 Java, 2 compte et dossiers, 3 jars, 4 configuration, 5 mot de passe, 6 service, 7 `journalctl`/`ss`, 8 mémoire, 9.3 redémarrage de `samae`, mises à jour et retour arrière de Lavalink | 🔑 propriétaire |
