# Assistant IA (lot 3) : `/ia question`, `/ia bilan`, `/ia relecture`

L'IA est **désactivée par défaut** (`AI_PROVIDER=none`) : le bot fonctionne sans clé.
Quand elle est activée, elle sert d'aide à l'équipe. Elle ne prend aucune décision, ne
modifie rien sur GitLab et ne publie rien dans les salons : toutes ses réponses sont
éphémères, visibles uniquement par la personne qui a lancé la commande.

## 1. Commandes

| Commande | Ce que l'IA reçoit | Ce qu'elle produit |
|---|---|---|
| `/ia question question:…` | La question, plus au plus 6 extraits trouvés dans les documents de l'équipe (voir §3) | Une réponse brève qui **cite ses sources** ([1], [2]…) ou dit qu'elle ne sait pas. Sans extrait correspondant, l'IA n'est pas appelée. |
| `/ia bilan [periode]` | Les événements GitLab enregistrés par le bot sur la période (300 au plus) et leurs statistiques | Un bilan en quatre parties : synthèse, avancées, points d'attention, pistes. Sans événement, l'IA n'est pas appelée. |
| `/ia relecture mr:12` | Le titre, la description et le diff de la merge request !12, lus sur GitLab **en lecture seule** (60 000 caractères au plus, 8 000 par fichier) | Une relecture : bugs probables, cas non gérés, sécurité, tests manquants, lisibilité, puis un verdict. **Une aide, pas une validation** : la relecture d'un membre reste nécessaire. |

Chaque réponse indique :

- les sources transmises ;
- les éventuelles coupes (diff tronqué, réponse coupée) ;
- le nombre de secrets masqués ;
- le modèle utilisé (« réponse générée par IA, à vérifier »).

## 2. Configuration (`.env`)

| Variable | Valeur | Rôle |
|---|---|---|
| `AI_PROVIDER` | `none` (défaut), `anthropic`, `fake` | `fake` donne des réponses de test fixes, sans clé ni appel réseau : pratique pour essayer les commandes. |
| `AI_API_KEY` | clé d'API Anthropic | Obligatoire avec `anthropic`. Créée sur console.anthropic.com > *API Keys*. **Secret** : seulement dans `.env`. |
| `AI_MODEL` | vide = `claude-opus-5-5` | Modèle Claude utilisé, par exemple `claude-sonnet-5-5` (moins cher) ou `claude-haiku-5-5` (bien moins cher, moins précis). |
| `AI_DAILY_LIMIT` | 50 | Nombre maximal de demandes par jour pour toute l'équipe (remise à zéro à minuit UTC). |
| `AI_COOLDOWN_SECONDS` | 20 | Délai minimal entre deux demandes d'un même membre. |
| `GITLAB_ACCESS_TOKEN` | token GitLab, scope `read_api` | Nécessaire uniquement pour `/ia relecture`. **Secret.** |
| `GITLAB_PROJECT_ID` | identifiant numérique | Nécessaire pour `/ia relecture`. Déjà utilisé par les webhooks. |
| `GITLAB_BASE_URL` | `https://gitlab.com` (défaut) | Adresse de l'instance GitLab. Une adresse non `https://` désactive l'API, pour que le token ne circule jamais en clair. |

Une valeur invalide (fournisseur inconnu, clé absente…) **désactive seulement l'IA** :
le reste du bot démarre, et `/etat` affiche la raison.

### Créer le token GitLab (lecture seule)

Dans le projet GitLab : *Settings > Access tokens > Add new token*.

- **Nom** : `sae-bot-lecture`.
- **Rôle** : `Reporter`.
- **Scope** : uniquement **`read_api`**.
- **Expiration** : fin de la SAE.

Collez la valeur dans `GITLAB_ACCESS_TOKEN` de `.env` sur la machine du bot, **jamais**
sur Discord. Le bot n'envoie le token que dans l'en-tête `PRIVATE-TOKEN` des requêtes
à GitLab, et ne l'affiche ni ne le journalise jamais.

### Coût

Chaque demande consomme des tokens facturés par Anthropic sur le compte de la clé.
Le bot conserve, pour chaque demande, la date, le membre, la commande, le modèle et
le nombre de tokens. Le texte des questions et des réponses n'est jamais conservé.
`/etat` affiche le nombre de demandes du jour. Le modèle travaille avec un effort
« medium », qui suffit pour résumer et relire.

Pour Claude Opus 5.5 (et Sonnet 5.5, Opus 5, Fable 5.1), le **repli côté serveur** est
activé (`fallbacks: "default"`). Si le modèle décline à tort une demande légitime (par
exemple un diff qui touche à la sécurité), l'API la confie automatiquement à un autre
modèle dans le même appel, au lieu de renvoyer un refus. Ce repli est facturé au tarif
du modèle de secours. Le retirer : choisir un `AI_MODEL` qui ne le prend pas en charge
(Haiku), ou modifier `FALLBACK_MODELS` dans `src/ai/anthropic_provider.py`.

## 3. Sources de `/ia question`

Dans l'ordre :

1. les fichiers `.md` et `.txt` du dossier `knowledge/` (voir `knowledge/README.md`) ;
2. `config/planning.toml` ;
3. `config/links.toml` (liens renseignés et liens manquants) ;
4. les messages permanents (`content/messages/`) ;
5. la documentation du bot (`README.md`, `docs/`).

La recherche compte les mots communs entre la question et chaque passage (sans accents
ni majuscules). Plus `knowledge/` est riche, meilleures sont les réponses.

## 4. Sécurité et confidentialité

- **Masquage avant envoi** : sont remplacés par `[secret masqué]` avant tout envoi à l'IA :
  - les secrets du `.env` ;
  - tout ce qui ressemble à un token ou une clé : tokens GitLab `glpat-`, clés `sk-`,
    tokens Discord, clés AWS, clés privées, `password = "…"`, lignes `API_KEY=…`.
  
  C'est un filet de sécurité, pas une garantie : ne commitez pas de secrets.
- Les extraits, les événements et le diff sont présentés à l'IA comme des **données** : un
  texte du type « ignore tes consignes » dans un commit ne change pas ses règles.
- Les réponses ne peuvent notifier personne (mentions désactivées) et restent éphémères.
- Quotas : une demande en cours à la fois par membre, un délai entre deux demandes d'un
  même membre (`AI_COOLDOWN_SECONDS`) et une limite quotidienne pour l'équipe
  (`AI_DAILY_LIMIT`). Une demande refusée par le quota ne coûte rien.
- Une erreur de l'IA (clé refusée, service indisponible…) s'affiche en une phrase, sans
  détail interne. La cause figure dans `logs/bot.log`, sans la clé.

## 5. Journal d'utilisation de l'IA (SAE)

La table `ai_requests` de la base SQLite garde la trace de chaque demande, sans son
contenu, pendant `HISTORY_RETENTION_DAYS` jours. Elle peut servir de base au journal
d'utilisation de l'IA demandé par l'EPITA (lien `journal_ia` de `config/links.toml`).
Le contenu des réponses, lui, reste sous la responsabilité de chaque membre qui
choisit de le réutiliser.
