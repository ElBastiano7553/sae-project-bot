# Parler à Claude Code depuis Discord (plugin officiel « discord »)

Ce guide sert à **Romain** : piloter sa session Claude Code (sur son PC) depuis son téléphone, par
messages privés Discord. C'est indépendant de Samae :

| | Plugin Discord de Claude Code | Samae (`/samae`, `/ia`) |
|---|---|---|
| Ce que c'est | un pont entre Discord et **une session Claude Code ouverte** | le bot de l'équipe, en service sur le VPS |
| Quand ça marche | seulement pendant que la session tourne sur le PC | 24 h/24 |
| Qui l'utilise | Romain (et les comptes qu'il autorise explicitement) | toute l'équipe |
| Ce qu'il peut faire | tout ce que la session Claude Code peut faire sur le PC (lire, modifier, lancer des commandes) | lire la base de Samae ; écrire seulement après validation |
| Coût | l'abonnement Claude de Romain | 0 € par défaut ; API à l'usage si l'IA est activée |

Il faut un **bot Discord séparé** : n'utilisez jamais le token de Samae ici.

## Ce qui a été vérifié (27/11/2026)

- Plugin `discord@claude-plugins-official`, dépôt `anthropics/claude-plugins-official`,
  `external_plugins/discord` : code lu (serveur MCP `server.ts` en TypeScript, `ACCESS.md`, relais
  des permissions). Il n'envoie rien ailleurs qu'à Discord.
- Il exige **Bun** (pas Node). Installé ici avec Bun 1.4.4 : le plugin s'installe, et le serveur refuse
  de démarrer sans token (« DISCORD_BOT_TOKEN required »), ce qui est attendu.
- Les « channels » de Claude Code sont une **préversion de recherche** : il faut lancer Claude Code
  avec l'option `--channels`. Il **ne tourne pas en arrière-plan** : fermer le terminal coupe le pont.
- Non testé : la connexion réelle à Discord et l'appairage (pas de token ni d'accès au PC depuis
  l'environnement de travail).

## Mise en place (Windows, PowerShell)

### 1. Créer le bot dédié

1. https://discord.com/developers/applications > **New Application** (par exemple « Claude de Romain »).
2. **Bot** > **Reset Token** : copiez le token (il ne sera plus affiché). Ne le collez nulle part
   ailleurs que dans l'étape 3.
3. Toujours dans **Bot** : décochez **Public Bot** (personne d'autre ne pourra l'inviter) et cochez
   **Message Content Intent** (le plugin doit lire vos messages).
4. Pour les messages privés seulement, aucune invitation n'est nécessaire au-delà d'un serveur commun :
   invitez-le sur un serveur où vous êtes (OAuth2 > URL Generator : `bot`, permissions « Envoyer des
   messages », « Lire l'historique », « Ajouter des réactions »). Un serveur privé à vous suffit.

### 2. Installer Bun et le plugin

```powershell
powershell -c "irm bun.sh/install.ps1 | iex"   # puis rouvrir PowerShell
bun --version
claude --version                                # Claude Code 2.1.x ou plus récent
```

Dans Claude Code :

```
/plugin install discord@claude-plugins-official
```

### 3. Donner le token

Dans Claude Code :

```
/discord:configure <token du bot dédié>
```

Le token est écrit dans `%USERPROFILE%\.claude\channels\discord\.env`, sur votre PC uniquement. Ne le
mettez pas dans un dépôt Git, ni dans un message.

### 4. Lancer avec le pont

```powershell
claude --channels plugin:discord@claude-plugins-official
```

ou le script fourni, qui vérifie Bun et Claude Code avant de lancer :

```powershell
.\deploy\claude-discord\lancer-claude-discord.ps1 -Dossier "C:\chemin\vers\sae-project-bot"
```

Sur un compte d'organisation Team ou Enterprise, un administrateur doit d'abord autoriser les channels
(réglage `channelsEnabled`) ; sur Pro ou Max, l'option `--channels` suffit.

### 5. Restreindre l'accès à vous seul

1. Envoyez un message privé au bot depuis votre compte Discord : il répond avec un **code d'appairage** à 6 caractères (le message lui-même n'est pas transmis).
2. Dans Claude Code : `/discord:access pair <code>`.
3. Puis **`/discord:access policy allowlist`** : seuls les comptes appairés sont écoutés, les autres
   messages sont ignorés sans réponse. Faites-le tout de suite.
4. Vérifiez avec `/discord:access` (politique, comptes autorisés, appairages en attente). Retirez un
   compte avec `/discord:access remove <identifiant Discord>`. L'état est dans
   `%USERPROFILE%\.claude\channels\discord\access.json`.

Salons de serveur (facultatif, déconseillé) : ils sont désactivés par défaut ; si vous en ajoutez un,
gardez `requireMention` (réponse seulement si le bot est mentionné), restreignez avec
`--allow <vos identifiants>` et choisissez un salon privé.

## Risques à connaître

- **Relais des permissions** : quand Claude Code demande une autorisation (lancer une commande, modifier
  un fichier), un compte autorisé peut l'accepter depuis Discord. Quiconque contrôle un compte
  autorisé contrôle donc votre PC dans les limites de la session. N'autorisez que votre propre compte,
  avec la double authentification Discord activée.
- Si le token fuite : portail développeur > Bot > **Reset Token**, puis `/discord:configure` avec le
  nouveau.
- Laisser la session ouverte longtemps : utilisez un terminal dédié et verrouillez la session Windows ;
  le PC doit rester allumé et connecté. Fermer le terminal arrête tout.
