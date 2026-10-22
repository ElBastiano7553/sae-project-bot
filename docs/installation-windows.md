# Guide d'installation sous Windows (V1 et V2)

Ce guide part de zéro. Les commandes sont à copier-coller dans le **terminal
PowerShell de VS Code**. La configuration des notifications GitLab (V2) est
dans [gitlab-webhooks.md](gitlab-webhooks.md) ; l'IA arrivera en V4.

> 🔒 **Règle d'or** : vos tokens et secrets ne doivent apparaître que dans le
> fichier `.env` de votre ordinateur. Ne les envoyez à personne, pas même à un
> assistant IA, et ne les collez jamais dans Discord ou GitLab.

---

## 1. Installer et vérifier Python

1. Téléchargez **Python 3.12** sur <https://www.python.org/downloads/windows/>
   (« Windows installer (64-bit) »).
2. Lancez l'installateur et **cochez « Add python.exe to PATH »** en bas de la
   première fenêtre, puis « Install Now ».
3. Ouvrez un nouveau PowerShell et vérifiez :

```powershell
py --version
py -0
```

**Résultat attendu** : `Python 3.12.x`, et `-V:3.12` dans la liste de `py -0`.

**Erreurs possibles**
- `py n'est pas reconnu` : réinstallez Python en cochant « Add python.exe to PATH »
  et « py launcher », puis fermez et rouvrez VS Code.
- Une autre version s'affiche : ce n'est pas grave, la commande `py -3.12` de
  l'étape 3 choisira la bonne. Python 3.11 et 3.13 fonctionnent aussi.

## 2. Ouvrir le projet dans VS Code

1. Installez VS Code (<https://code.visualstudio.com/>) puis l'extension
   **Python** de Microsoft (VS Code la propose aussi à l'ouverture du projet).
2. Récupérez le projet : soit en dézippant l'archive fournie, soit avec Git :

```powershell
git clone https://github.com/ElBastiano7553/sae-project-bot.git
```

3. Dans VS Code : **Fichier > Ouvrir le dossier…** et choisissez le dossier
   `sae-project-bot` (celui qui contient `README.md`).
4. Ouvrez le terminal : **Terminal > Nouveau terminal**. Vérifiez que vous êtes
   dans le bon dossier :

```powershell
Get-ChildItem README.md
```

## 3. Créer et activer l'environnement virtuel

Un environnement virtuel isole les bibliothèques du projet du reste de votre PC.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

**Résultat attendu** : le début de la ligne du terminal affiche `(.venv)`.

**Erreur fréquente** : « l'exécution de scripts est désactivée sur ce système ».
Autorisez les scripts pour votre utilisateur uniquement, puis réessayez :

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
.\.venv\Scripts\Activate.ps1
```

Dans VS Code, choisissez aussi l'interpréteur : `Ctrl+Maj+P` > « Python: Select
Interpreter » > celui qui contient `.venv`. Les prochains terminaux s'activeront
automatiquement.

> À chaque nouvelle session de travail, réactivez l'environnement avec
> `.\.venv\Scripts\Activate.ps1` si `(.venv)` n'apparaît pas.

## 4. Installer les dépendances

```powershell
python -m pip install --upgrade pip
pip install -r requirements-dev.txt
```

Puis lancez les tests (aucun token n'est nécessaire) :

```powershell
python -m pytest
```

**Résultat attendu** : une ligne finale du type `81 passed`.

**Erreurs possibles**
- `No module named pytest` : l'environnement virtuel n'est pas activé (étape 3).
- Erreur de compilation lors de l'installation : vous utilisez probablement une
  version de Python très récente ou très ancienne ; utilisez Python 3.12.

## 5. Créer l'application et le bot Discord

1. Allez sur <https://discord.com/developers/applications> et connectez-vous.
2. **New Application**, nommez-la par exemple `SAE Project Bot`, acceptez les
   conditions, **Create**.
3. Menu de gauche **Bot** :
   - donnez un nom et éventuellement un avatar ;
   - cliquez **Reset Token**, confirmez, puis **Copy**. Collez ce token
     **uniquement** dans votre fichier `.env` (étape 8). Discord ne le réaffichera
     plus : s'il est perdu ou divulgué, refaites « Reset Token » ;
   - désactivez **Public Bot** pour que personne d'autre ne puisse l'inviter.

## 6. Intents et permissions

**Intents** (page **Bot**, section « Privileged Gateway Intents ») : la V1
n'en a besoin d'**aucun**. Laissez *Presence*, *Server Members* et *Message
Content* désactivés.

**Permissions du bot** (choisies à l'étape 7) :

| Permission | Pourquoi |
|---|---|
| Voir les salons | trouver les salons du serveur |
| Envoyer des messages | publier les messages permanents |
| Intégrer des liens | afficher les embeds |
| Voir les anciens messages | vérifier qu'un message permanent existe encore |
| Épingler des messages | épingler les messages permanents (facultatif) |

Pas de permission **Administrateur**, ni « Gérer les salons », ni « Gérer les
rôles » : le bot ne crée et ne supprime rien.

> Si « Épingler des messages » n'apparaît pas dans la liste, ne cochez pas
> « Gérer les messages » pour autant : mettez simplement
> `PIN_PERMANENT_MESSAGES=false` dans `.env`.

## 7. Inviter le bot sur le serveur « SAE · GAME DEV »

1. Menu **OAuth2 > URL Generator**.
2. **Scopes** : cochez `bot` et `applications.commands`.
3. **Bot Permissions** : cochez les permissions du tableau de l'étape 6.
4. Copiez l'URL générée en bas, ouvrez-la dans le navigateur, choisissez le
   serveur « SAE · GAME DEV » et **Autoriser**. Il faut avoir la permission
   « Gérer le serveur » sur ce serveur.

**Salons privés** : si certains salons ne sont visibles que par certains rôles,
ajoutez le rôle du bot (il porte le nom du bot) dans les permissions de ces
salons ou de leur catégorie.

## 8. Configurer le fichier .env

```powershell
Copy-Item .env.example .env
code .env
```

Remplissez au minimum :

```dotenv
DISCORD_BOT_TOKEN=collez-ici-le-token-du-bot
DISCORD_GUILD_ID=123456789012345678
```

Pour obtenir `DISCORD_GUILD_ID` : dans Discord, **Paramètres utilisateur >
Avancés > Mode développeur** (activer), puis clic droit sur l'icône du serveur >
**Copier l'identifiant du serveur**.

Facultatif : `ADMIN_ROLE_NAME=Chef de projet` pour autoriser un rôle à utiliser
les commandes d'administration (`/setup`, `/refresh`, `/config`, `/announce`,
`/admin …`) ; sinon, seuls les membres ayant « Gérer le serveur » y ont accès.

Enregistrez (`Ctrl+S`). Le fichier `.env` est ignoré par Git : il ne sera jamais
publié. Vérifiez-le à tout moment avec :

```powershell
git check-ignore .env
```

(qui doit afficher `.env`).

### Les trois secrets du projet, à ne pas confondre

| Secret | Sert à | Où le créer | Étape |
|---|---|---|---|
| `DISCORD_BOT_TOKEN` | permettre au programme de se connecter **en tant que bot** à Discord. Quiconque l'obtient contrôle le bot. | Portail développeur Discord > Bot > Reset Token | V1 |
| `GITLAB_WEBHOOK_SECRET` | un mot de passe que **GitLab envoie** avec chaque notification, pour prouver au bot qu'elle vient bien de votre projet. Vous l'inventez vous-même. | Vous le générez et le saisissez dans GitLab et dans `.env` | V2 |
| `GITLAB_ACCESS_TOKEN` | permettre au **bot de lire** l'API GitLab (Issues, MR, pipelines) pour les bilans. Doit être en **lecture seule** (`read_api`). | GitLab > Paramètres > Jetons d'accès | V3 |

## 9. Lancer le bot

```powershell
python -m src.main
```

**Résultat attendu** (exemple) :

```
... | INFO     | src.database.connection | Migration de la base de données vers la version 1
... | INFO     | discord.client | logging in using static token
... | INFO     | src.discord_bot.bot | 9 commandes slash synchronisées sur le serveur 1234...
... | INFO     | src.discord_bot.bot | Connecté en tant que SAE Project Bot#1234 (id ...)
... | INFO     | src.discord_bot.bot | Structure : 19/19 salons utilisables
```

Le bot reste lancé tant que le terminal est ouvert. Pour l'arrêter : `Ctrl+C`.
Les journaux sont aussi écrits dans `logs/bot.log`.

Dans Discord, tapez `/status` (ou son ancien nom `/health`) : un embed « État du
bot » doit s'afficher (visible par vous seul). `/help` liste toutes les commandes.

## 10. Tester /setup en mode prévisualisation

Tapez `/setup`. Le bot répond (visible par vous seul) avec :

- **Structure du serveur** : un ✅ par salon reconnu, ⚠️ si un salon est dans une
  autre catégorie que prévu, ❌ s'il est absent, en double ou du mauvais type ;
- **Messages permanents prévus** : 🆕 création, ✏️ mise à jour, ✅ déjà à jour.

**À ce stade, rien n'a été modifié.** Cliquez **Annuler** si quelque chose ne va
pas, corrigez (nom d'un salon dans Discord ou dans `config/server.toml`) et
relancez `/setup`.

## 11. Créer les messages permanents

1. Relancez `/setup` et cliquez **Confirmer** dans les 2 minutes.
2. Le bot publie les 7 messages dans `#bienvenue` (bienvenue + liens utiles),
   `#annonces` (règles + annonce en cours), `#planning-et-deadlines`,
   `#decisions-et-comptes-rendus` et `#documentation-et-preuves`, puis les épingle.
   Tant qu'un lien de `config/links.toml` est vide, le message concerné affiche
   une section « 🛠️ À compléter » (lien obligatoire ou facultatif) : c'est normal.
3. Lancez `/refresh` : le rapport doit indiquer **0 message modifié** et tous les
   messages « à jour ». C'est la preuve qu'il n'y a pas de doublon.
4. Test de recréation : supprimez le message de `#bienvenue` : le bot le recrée
   tout seul au bout de quelques secondes (désactivable avec
   `AUTO_RECREATE_DELETED_MESSAGES=false`, il faut alors lancer `/refresh`).
5. Test de modification : renseignez un lien dans `config/links.toml` ou une
   échéance dans `config/planning.toml`, puis `/refresh` : le message est modifié
   sur place (✏️), sans nouveau message.

6. Test d'annonce : `/announce`, remplissez le formulaire, vérifiez la
   prévisualisation puis **Confirmer** : le message « Annonce en cours » de
   `#annonces` est modifié sur place. Refaites `/announce` : toujours un seul message.
7. `/admin messages` résume l'état de chaque message ; `/admin diagnostic` vérifie
   les permissions du bot dans chaque salon.

Les textes se modifient dans `content/messages/*.toml`. Les échéances ne sont
jamais inventées : elles viennent uniquement de `config/planning.toml`.

## 12. Notifications GitLab (V2)

Une fois les étapes 1 à 11 validées, suivez le guide détaillé
[gitlab-webhooks.md](gitlab-webhooks.md). En résumé, dans l'ordre :

1. générer un secret (`python -c "import secrets; print(secrets.token_urlsafe(32))"`)
   et le mettre dans `GITLAB_WEBHOOK_SECRET` du fichier `.env`, puis relancer le bot
   (**terminal 1**) : `/status` doit afficher GitLab ✅ ;
2. tester sans GitLab (**terminal 3**) : `python -m src.gitlab.demo push` doit
   publier un exemple dans `#gitlab-activite`, et `--wrong-secret` doit être refusé (401) ;
3. installer et lancer le tunnel HTTPS (**terminal 2**) :
   `winget install --id Cloudflare.cloudflared`, rouvrir VS Code, puis
   `cloudflared tunnel --url http://127.0.0.1:8080` et noter l'adresse
   `https://….trycloudflare.com` affichée ;
4. vérifier le tunnel : `curl.exe -i https://ADRESSE/webhooks/gitlab` doit répondre 405 ;
5. créer le webhook dans GitLab (Settings > Webhooks) avec l'URL
   `https://ADRESSE/webhooks/gitlab` et le même secret, puis **Test > Push events** ;
6. dérouler le tableau « Tests réels, à cocher » du guide.

Tant que `GITLAB_WEBHOOK_SECRET` est vide, la V2 reste désactivée. Une erreur de
configuration GitLab désactive seulement la V2 : la V1 continue de fonctionner.

## 13 et 14. Commandes GitLab et IA

Elles seront ajoutées avec les versions V3 (commandes de consultation) et V4 (IA).
Ne configurez pas encore `GITLAB_ACCESS_TOKEN` ni les variables `AI_*`.

## 15. Diagnostiquer les erreurs fréquentes

| Symptôme | Cause probable | Solution |
|---|---|---|
| `Configuration invalide : DISCORD_BOT_TOKEN est vide` | `.env` absent ou mal nommé | Vérifiez que le fichier s'appelle exactement `.env` (pas `.env.txt`) et qu'il est à la racine du projet. |
| `Connexion refusée : DISCORD_BOT_TOKEN est invalide` | token mal copié ou régénéré | Refaites « Reset Token » et recopiez-le sans espace ni guillemets. |
| `Impossible de joindre Discord` | pas d'Internet, proxy ou pare-feu (réseau de l'école) | Testez avec un autre réseau (partage de connexion). |
| `Le bot n'est pas présent sur le serveur` | mauvais `DISCORD_GUILD_ID` ou bot non invité | Recopiez l'identifiant du serveur, refaites l'étape 7. |
| Les commandes `/` n'apparaissent pas | scope `applications.commands` manquant, ou Discord pas encore actualisé | Réinvitez le bot avec les deux scopes ; redémarrez Discord (`Ctrl+R`). |
| `⛔ Cette commande est réservée aux administrateurs` | vous n'avez ni « Gérer le serveur » ni le rôle `ADMIN_ROLE_NAME` | Demandez ce rôle ou renseignez `ADMIN_ROLE_NAME`. |
| ❌ `salon introuvable` dans `/setup` | nom différent de `config/server.toml` | Corrigez le nom dans Discord ou dans `config/server.toml`. Les emojis et accents sont ignorés. |
| ❌ `n salons portent ce nom` | doublon | Renommez ou supprimez le doublon vous-même : le bot ne supprime rien. |
| `le bot ne peut pas écrire dans #…` | permissions du salon | Donnez au rôle du bot « Voir le salon », « Envoyer des messages » et « Intégrer des liens » sur ce salon. |
| `épinglage impossible` | permission « Épingler des messages » absente | Ajoutez-la, ou mettez `PIN_PERMANENT_MESSAGES=false`. |
| `Erreur de syntaxe dans planning.toml` | TOML mal écrit | Les dates s'écrivent `2026-11-15` sans guillemets, les textes entre guillemets. |
| `l'exécution de scripts est désactivée` | politique PowerShell | Voir l'étape 3. |

En cas de doute, regardez `logs/bot.log` : chaque erreur y est détaillée. Si vous
partagez ce fichier, relisez-le d'abord : il ne contient pas le token, mais
vérifiez qu'aucune information privée n'y figure.
