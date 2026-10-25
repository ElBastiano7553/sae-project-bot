# Installer Samae sur une nouvelle machine (tout système, version 1.12.0)

Ce guide installe Samae **de zéro**, sur n'importe quel ordinateur ou serveur, pour
n'importe quel serveur Discord. Aucun identifiant de serveur, de salon ou de rôle n'est à
écrire dans le code : **seul le token du bot est obligatoire**, tout le reste se règle
ensuite depuis Discord avec `/setup`.

Guide pas à pas pour Windows et VS Code : [installation-windows.md](installation-windows.md).
Hébergement permanent sur un VPS : [deployment-vps.md](deployment-vps.md).

> 🔒 **Règle d'or** : le token du bot ne va **que** dans le fichier `.env` de la machine
> (ignoré par Git). Jamais dans un fichier `.py`, jamais sur Discord, jamais dans une capture
> d'écran, jamais à un assistant IA. S'il a fuité : portail développeur > Bot > **Reset Token**.

## 1. Prérequis

| Quoi | Version | Obligatoire ? |
|---|---|---|
| Python | 3.11, 3.12 ou 3.13 | oui |
| Git | n'importe laquelle | conseillé (sinon : archive ZIP du dépôt) |
| Java + Lavalink | Java 17 ou plus, Lavalink **4.2.2** | seulement pour la musique (§6) |

Tout est gratuit ; aucune dépendance payante.

## 2. Créer le bot sur Discord (5 minutes)

1. <https://discord.com/developers/applications> > **New Application** > un nom (ex. « Samae test »).
2. Onglet **Bot** :
   - **Reset Token** > copiez le token (gardez-le pour l'étape 4, ne le collez nulle part ailleurs) ;
   - décochez **Public Bot** (sinon n'importe qui peut inviter votre bot) ;
   - **Privileged Gateway Intents** : rien à cocher. Seulement si vous activez l'accueil des
     nouveaux membres (`WELCOME_ON_JOIN=true`) : cochez « Server Members Intent ».
3. Onglet **OAuth2 > URL Generator** : cochez les scopes **bot** et **applications.commands**,
   puis les permissions ci-dessous, et copiez l'URL générée en bas de page.

| Permission | Pour |
|---|---|
| View Channels, Send Messages, Embed Links, Read Message History | répondre, panneaux (musique, réunions) |
| Connect, Speak | musique |
| Manage Messages | épingler les messages permanents (espace d'équipe, facultatif) |
| Manage Channels, Manage Roles | `/refonte` seulement (espace d'équipe, facultatif) |

Lien d'invitation **minimal** (réponses, panneaux, musique), à compléter avec votre
identifiant d'application (onglet General Information > Application ID) :

```
https://discord.com/oauth2/authorize?client_id=<APPLICATION_ID>&scope=bot+applications.commands&permissions=3230720
```

Le bot ne demande jamais la permission Administrateur.

## 3. Télécharger le code et installer les dépendances

```bash
git clone https://github.com/ElBastiano7553/sae-project-bot.git
cd sae-project-bot
python3 -m venv .venv
source .venv/bin/activate            # Windows : .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt      # versions figées
```

Vérification (facultative, sans token ni Discord) :

```bash
pip install -r requirements-dev.txt
python -m pytest -q                       # toute la suite
python -m pytest -m "not integration" -q  # tests unitaires seulement
```

## 4. Remplir `.env`

```bash
cp .env.example .env                  # Windows : Copy-Item .env.example .env
```

Ouvrez `.env` et remplissez **une seule ligne** :

```
DISCORD_BOT_TOKEN=<le token de l'étape 2>
```

Tout le reste est facultatif et désactivé par défaut. `git status` ne doit **pas** montrer
`.env` (il est dans `.gitignore`).

## 5. Lancer, inviter, utiliser

```bash
python -m src.main
```

Au démarrage, le bot :

- vérifie `.env` : une valeur **obligatoire** manquante ou invalide arrête le bot avec un
  message clair (code 2) ; une fonction **facultative** mal configurée est seulement
  désactivée, avec la raison dans le journal et dans `/etat` ;
- écrit la liste des fonctions facultatives et leur état (« Musique : désactivée
  (MUSIC_ENABLED=false) »…), sans jamais écrire de secret ;
- crée `data/` et la base SQLite s'ils n'existent pas, et applique les migrations (avec une
  sauvegarde vérifiée avant chaque migration d'une base existante) ;
- envoie les commandes slash à Discord **seulement si elles ont changé**.

Ouvrez le lien d'invitation de l'étape 2, choisissez votre serveur, puis sur Discord :

1. `/setup etat` (admins) : ce qui fonctionne sur ce serveur, ce qui manque et comment le régler ;
2. `/aide`, `/reunion creer`, `/projet liste`, `/etat` : utilisables tout de suite ;
3. musique : voir §6.

Les commandes globales peuvent mettre quelques minutes à apparaître la toute première fois
(Discord). Redémarrez l'application Discord si elles n'apparaissent pas.

## 6. Activer la musique (facultatif)

1. Installer Java 17+ et télécharger Lavalink 4.2.2 et le plugin youtube 1.18.2 (liens et
   empreintes SHA-256 : [music-vps-checklist.md](music-vps-checklist.md), étape 3).
2. Copier `deploy/lavalink/application.yml.example` en `application.yml` à côté du jar.
3. Générer un mot de passe : `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
4. Lancer Lavalink : `LAVALINK_SERVER_PASSWORD=<mot de passe> java -Xmx512m -jar Lavalink.jar`.
5. Dans `.env` : `MUSIC_ENABLED=true` et `LAVALINK_PASSWORD=<le même mot de passe>`, puis relancer le bot.
6. Sur Discord : rejoindre un salon vocal, `/play query:<titre ou lien>`. Pas de son ?
   `/audiocheck` et [music.md](music.md), §8 bis.
7. Facultatif : `/setup role-dj role:@DJ` pour réserver « passer le titre d'un autre »,
   « arrêter » et « volume » à un rôle sur ce serveur.

## 7. Espace d'équipe (facultatif)

L'espace d'équipe (messages permanents, `/admin`, `/refonte`, `/ia`, `/activite`, `/roles`,
webhooks GitLab/GitHub du `.env`) n'existe que sur **un** serveur : le **serveur principal**.

- Soit `DISCORD_GUILD_ID=<identifiant du serveur>` dans `.env` (comme avant 1.12.0) ;
- soit, `DISCORD_GUILD_ID` vide, `/setup serveur-principal` sur le serveur voulu. Seul le
  **propriétaire du bot** (le compte qui a créé l'application à l'étape 2) peut le faire :
  l'administrateur d'un autre serveur ne peut jamais s'approprier l'espace d'équipe.

Ensuite : `/admin installer` sur ce serveur. Sur les autres serveurs, ces commandes
n'apparaissent pas ; la musique, les réunions et `/projet` y fonctionnent normalement, avec
des données séparées par serveur.

## 8. Que faire si…

| Message | Cause | Solution |
|---|---|---|
| `DISCORD_BOT_TOKEN est vide` | `.env` absent ou ligne vide | étape 4 (le fichier s'appelle `.env`, pas `.env.txt`) |
| `Connexion refusée : DISCORD_BOT_TOKEN est invalide` | token copié en partie, ou régénéré depuis | Reset Token et recopier |
| `DISCORD_GUILD_ID doit contenir uniquement des chiffres, ou rester vide` | nom du serveur au lieu de son identifiant | laisser vide, ou copier l'identifiant (mode développeur) |
| Les commandes n'apparaissent pas | Discord met en cache | attendre quelques minutes, redémarrer Discord ; `python -m src.ops resync` puis relancer le bot |
| « Cette commande fait partie de l'espace d'équipe » | commande d'équipe hors du serveur principal | §7 |
| La musique répond « pas activée » | `MUSIC_ENABLED=false` | §6 |
