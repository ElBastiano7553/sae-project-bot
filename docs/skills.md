# Skills Claude Code du dépôt

Les Skills sont des fiches d'instructions que Claude Code charge quand une tâche s'y prête. Ceux de ce
dépôt sont dans `.claude/skills/` et ne servent **qu'à Claude Code** (ils n'ont aucun effet sur Samae en
production). Provenance, commits et licences : [.claude/skills/SOURCES.md](../.claude/skills/SOURCES.md).

## Installés

| Skill | Pourquoi |
|---|---|
| `samae-project` | écrit pour ce dépôt : architecture en couches, migrations, aide générée, règles de l'équipe (validation humaine, aucune donnée inventée, rien de destructif). C'est la référence en cas de conflit |
| `discord-bot-architect` (sickn33/agentic-awesome-skills) | bonnes pratiques générales de bots Discord (intents minimaux, interactions, limites). Ses exemples sont en Pycord : les règles de `samae-project` et le code (discord.py) priment |
| `python-testing-patterns` (même dépôt) | organisation des tests pytest, fixtures, tests asynchrones |

Les trois ont été lus en entier avant installation : uniquement du Markdown, aucun script, aucune
commande réseau. Pour vérifier qu'ils sont détectés : lancer `claude` dans le dossier du dépôt, puis
`/skills`.

## Examinés et écartés

| Candidat | Raison |
|---|---|
| Nice-Wolf-Studio/agent-discord-skills | un seul commit, peu d'activité ; commandes `curl` qui placent le token du bot dans le shell ; actions destructrices (suppression de salons) ; redondant avec le plugin officiel |
| `discord-automation` (sickn33) | passe par un service tiers (Composio / Rube MCP) : données de l'équipe envoyées hors de nos outils |
| `xlsx` (sickn33) | dossier vide ; le Skill intégré `anthropic-skills:xlsx` existe déjà |

## Plugins officiels

- `discord@claude-plugins-official` : installé et testé à blanc pour l'usage « parler à Claude Code depuis
  Discord », voir [claude-code-discord.md](claude-code-discord.md). Il s'installe sur le PC de Romain,
  pas dans le dépôt.
- Recommandés mais non imposés (à installer par chacun s'il le souhaite) : `pyright-lsp` (vérification
  de types pendant l'édition) et `security-guidance` (relecture sécurité ; il fait lui-même des appels à
  un modèle et des `pip install`, d'où le choix de ne pas l'activer d'office).

## Ajouter ou mettre à jour un Skill

1. Lire tout le contenu (y compris scripts et références) et vérifier la licence.
2. Copier le dossier dans `.claude/skills/<nom>/` sans le modifier, noter le commit dans `SOURCES.md`.
3. Vérifier le frontmatter (`name`, `description`) et la détection avec `/skills`.
