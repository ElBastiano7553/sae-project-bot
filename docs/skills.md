# Skills Claude Code du dépôt

Les Skills sont des fiches d'instructions que Claude Code charge quand une tâche s'y prête. Ceux de ce
dépôt sont dans `.claude/skills/` et ne servent **qu'à Claude Code** (ils n'ont aucun effet sur Samae en
production). Provenance, commits et licences : [.claude/skills/SOURCES.md](../.claude/skills/SOURCES.md).

## Installés

| Skill | Pourquoi |
|---|---|
| `samae-project-context` | écrit pour ce dépôt : architecture en couches, conventions, règles de l'équipe. À lire avant toute modification importante ; c'est la référence en cas de conflit |
| `samae-game-design` | écrit pour ce dépôt : structure et règles du classeur « GAME DESIGN DATABASE — LUNASTEH », vérification |
| `samae-discord-ux` | écrit pour ce dépôt : nommage des commandes, embeds, composants, erreurs, confirmations |
| `samae-quality-assurance` | écrit pour ce dépôt : tests à écrire, vérifications, revue, compte rendu honnête |
| `discord-bot-architect` (sickn33/agentic-awesome-skills) | bonnes pratiques générales de bots Discord (intents minimaux, interactions, limites). Exemples en Pycord : les skills `samae-*` et le code (discord.py) priment |
| `python-testing-patterns` (même dépôt) | organisation des tests pytest, fixtures, tests asynchrones |
| `async-python-patterns` (même dépôt, 1.15.0) | asyncio : tâches, annulation, délais, files, verrous ; le cœur d'un bot Discord |

Tous ont été lus en entier avant installation : uniquement du Markdown, aucun script, aucune commande
réseau. Détection : lancer `claude` dans le dossier du dépôt, puis `/skills`.

## Outils intégrés à Claude Code utilisés à la place d'un skill

| Besoin | Outil intégré | Pourquoi pas un skill externe |
|---|---|---|
| Revue de code | `/code-review` | Déjà fourni, maintenu par Anthropic |
| Audit de sécurité | `/security-review` | Idem ; `security-audit-static` (sickn33) fait la même chose en lançant des sous-agents sur le modèle le plus cher |
| Lisibilité | `/simplify` | Idem |
| Git, commits, PR, GitHub Actions | outils intégrés (`git`, `/ship`), skill `samae-quality-assurance` | Les dizaines de skills `git-*` et `github-*` du catalogue (3062 skills) sont redondants |
| Excel | `anthropic-skills:xlsx` (intégré) + `samae-game-design` | Le skill `xlsx` du catalogue sickn33 était vide en novembre |
| Créer ou améliorer un skill | `anthropic-skills:skill-creator` | Intégré |

## Examinés et écartés

| Candidat | Raison |
|---|---|
| Nice-Wolf-Studio/agent-discord-skills | un seul commit ; commandes `curl` qui placent le token du bot dans le shell ; actions destructrices (suppression de salons) ; redondant avec le plugin officiel |
| `discord-automation` (sickn33) | passe par un service tiers (Composio / Rube MCP) : données de l'équipe envoyées hors de nos outils |
| `xlsx` (sickn33) | dossier vide ; le skill intégré `anthropic-skills:xlsx` existe déjà |
| `python-pro` (sickn33) | marqué `risk: critical` par le catalogue ; vise Python 3.12+, FastAPI, Django, uv : hors de notre pile (3.11 minimum, discord.py) |
| `security-audit-static` (sickn33, MIT) | contenu sain, mais redondant avec `/security-review` et coûteux (sous-agents sur le modèle le plus puissant) |
| `code-review-*`, `git-*`, `github-*` (sickn33) | redondants entre eux et avec les outils intégrés |

## Plugins officiels (anthropics/claude-plugins-official)

Le catalogue officiel est déjà connu de Claude Code sur le PC de Romain. Seul `cloudflare` y est installé.

| Plugin | Décision |
|---|---|
| `discord` | **Non installé sur ce PC** : il exige Bun (absent) et un **bot Discord dédié** avec son token et l'intent « Message Content ». Le créer et l'appairer demande Romain. Guide pas à pas : [claude-code-discord.md](claude-code-discord.md). Il ne fonctionne que pendant une session Claude Code ouverte |
| `security-guidance` | Non activé d'office : relecture par un modèle à chaque fin de tour et à chaque commit (consomme l'abonnement) et `pip install` automatiques. À activer par chacun : `/plugin install security-guidance@claude-plugins-official` |
| `pyright-lsp` | Recommandé, facultatif : vérification de types pendant l'édition ; demande `npm install -g pyright` |
| `github` (externe) | Non installé : demande un jeton GitHub personnel ; `git` suffit pour notre usage |
| `commit-commands`, `pr-review-toolkit`, `code-review` | Redondants avec les commandes intégrées |

## Ajouter ou mettre à jour un Skill

1. Lire tout le contenu (y compris scripts et références) et vérifier la licence.
2. Copier le dossier dans `.claude/skills/<nom>/` sans le modifier, noter le commit dans `SOURCES.md`.
3. Vérifier le frontmatter (`name`, `description`) et la détection avec `/skills`.
