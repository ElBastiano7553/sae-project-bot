# Samae assistant de projet : `/samae` (1.14.0)

`/samae demande:…` répond en français aux questions courantes sur le projet. La plupart des réponses
viennent **directement de la base de Samae**, sans IA ni coût. L'IA (Claude, par l'API Anthropic)
n'intervient que pour les questions libres, et seulement si elle est configurée.

> À ne pas confondre avec le **plugin Discord de Claude Code** ([claude-code-discord.md](claude-code-discord.md)),
> qui permet à Romain de parler à *sa* session Claude Code depuis son téléphone. Ce plugin ne tourne que
> pendant qu'une session Claude Code est ouverte sur son PC : ce n'est pas un service permanent et il ne
> remplace pas Samae.

## Ce que `/samae` comprend

| Exemple de demande | Réponse | Source | IA |
|---|---|---|---|
| « Quelles fonctionnalités restent à développer ? » | fonctionnalités non terminées | onglet FEATURES du suivi | non |
| « Fais-moi le bilan de notre avancement. » | x/y terminées, %, questions ouvertes, propositions en attente | suivi | non |
| « Quelles sont les dernières décisions prises ? » | décisions du suivi, des réunions et des débats | suivi + réunions + débats | non |
| « Prépare notre prochaine réunion. » | ordre du jour proposé : idées à classer, débats ouverts, actions en retard, questions ouvertes | idées, tier lists, débats, réunions | non |
| « Quelles idées sont encore en attente ? » | idées en attente | idées | non |
| « Résume les discussions de cette semaine. » | réunions, décisions, idées et débats des 7 derniers jours | base de Samae | non |
| « Ajoute cette idée : Boss volant — il change de phase » | formulaire **prérempli** ; rien n'est enregistré tant que le membre ne l'a pas validé | — | non |
| « Quelles questions restent ouvertes ? » | OPEN_QUESTIONS + débats non tranchés | suivi + débats | non |
| « cherche inventaire » | lignes du suivi et idées qui contiennent le mot | suivi + idées | non |
| toute autre question | réponse rédigée par Claude à partir de l'état du projet, **ou** liste de ce que Samae sait faire si l'IA est désactivée | état du projet + documents | oui, si configurée |

Règles :

- **Lecture automatique, écriture validée.** `/samae` ne modifie jamais rien lui-même : l'ajout d'idée
  passe par le formulaire `/idee proposer`, les changements du suivi par une proposition validée par un
  administrateur (`/suivi valider`), les décisions par `/debat confirmer`.
- Samae **ne lit pas les messages des salons** (pas d'intent « Message Content », privilégié) : le
  « résumé de la semaine » porte sur ce qui est enregistré dans Samae (réunions, décisions, idées,
  débats), pas sur les conversations.
- Option `ASSISTANT_ON_MENTION=true` : répondre aussi quand on mentionne `@Samae` (serveur principal
  seulement, 6 réponses par minute au plus). Discord donne le texte d'un message qui mentionne le bot
  même sans l'intent privilégié. Désactivée par défaut.

Le code : `src/services/project_assistant.py` (reconnaissance des demandes et réponses, sans
discord.py), `src/discord_bot/cogs/assistant.py` (commande et formulaire prérempli),
`AIAssistant.project()` dans `src/services/ai_assistant.py` (question libre).

## Architecture retenue : intermédiaire (règles + IA facultative)

| Option | Principe | Permanent ? | Coût | Verdict |
|---|---|---|---|---|
| **API seule** | chaque `/samae` envoie la question à Claude, qui répond à partir de l'état du projet | oui (Samae tourne sur le VPS) | chaque question payée, même « bilan » | trop cher pour des questions dont la réponse est déjà en base |
| **MCP** | Samae expose ses données comme serveur MCP ; un client Claude (Claude Code, Claude Desktop) les interroge | **non** : n'existe que pendant qu'un client est ouvert sur un poste | abonnement Claude de la personne | utile à Romain sur son PC, pas un assistant pour l'équipe sur Discord |
| **Intermédiaire** (retenue) | les demandes reconnues sont traitées par des règles sur la base ; seules les questions libres vont à Claude, si l'IA est activée | oui | 0 € pour les demandes reconnues ; à l'usage pour les questions libres | **retenue** : gratuite par défaut, fiable, sans halluciner les chiffres |

Avec l'architecture retenue, Samae fonctionne **entièrement sans clé d'API**. Aucun abonnement n'a été
souscrit. Activer l'IA est un choix de l'équipe, réversible à tout moment (`AI_PROVIDER=none`).

## Coûts si l'IA est activée

Tarifs publics Anthropic (par million de jetons, novembre 2026) :

| Modèle (`AI_MODEL`) | Entrée | Sortie | Lecture du cache |
|---|---|---|---|
| `claude-opus-5-5` (défaut) | 4 $ | 20 $ | 0,20 $ |
| `claude-sonnet-5-5` | 2 $ | 10 $ | 0,10 $ |
| `claude-haiku-5-5` | 0,10 $ | 0,50 $ | 0,01 $ |

Une question libre envoie environ 3 000 jetons (consigne, état du projet, extraits de documents) et en
reçoit environ 500 :

| Modèle | Par question | 50 questions/jour (quota par défaut) pendant 30 jours |
|---|---|---|
| Opus 5.5 | ≈ 0,022 $ | ≈ 33 $ au maximum |
| Sonnet 5.5 | ≈ 0,011 $ | ≈ 16 $ au maximum |
| Haiku 5.5 | ≈ 0,0006 $ | ≈ 1 $ au maximum |

Ce sont des plafonds : le quota `AI_DAILY_LIMIT` bloque au-delà, et en pratique une équipe de 5 pose
bien moins de 50 questions libres par jour. Pour un usage étudiant, **Haiku 5.5 suffit** pour ce type de
réponses courtes ; Opus 5.5 n'est utile que pour `/ia relecture`. Les demandes reconnues (bilan,
fonctionnalités, décisions…) ne coûtent rien quel que soit le modèle.

Limites et performances : une question libre prend en général 2 à 10 secondes (Samae affiche
« réfléchit… » pendant ce temps) ; une demande reconnue répond en moins d'une seconde. L'API applique
ses propres limites de débit selon le niveau du compte ; Samae ajoute un délai par membre
(`AI_COOLDOWN_SECONDS`) et une seule demande en cours par membre.

## Variables d'environnement

| Variable | Défaut | Rôle |
|---|---|---|
| `AI_PROVIDER` | `none` | `none` : IA désactivée ; `anthropic` : Claude ; `fake` : réponses de test sans réseau |
| `AI_API_KEY` | vide | clé de console.anthropic.com (seulement si `anthropic`) ; jamais affichée ni journalisée |
| `AI_MODEL` | `claude-opus-5-5` | `claude-haiku-5-5` recommandé pour `/samae` |
| `AI_DAILY_LIMIT` | `50` | demandes IA par jour pour toute l'équipe (toutes commandes IA confondues) |
| `AI_COOLDOWN_SECONDS` | `20` | délai entre deux demandes d'un même membre |
| `ASSISTANT_ON_MENTION` | `false` | répondre aussi aux mentions de `@Samae` |

Pour activer : créer une clé sur console.anthropic.com (avec une limite de dépense mensuelle réglée dans
la console), la mettre dans `.env` sur le VPS, puis redémarrer le service. Guide général : [ai.md](ai.md).
