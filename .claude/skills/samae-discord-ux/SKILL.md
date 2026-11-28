---
name: samae-discord-ux
description: Règles d'expérience utilisateur Discord de Samae (discord.py) : nommage des commandes slash, embeds, boutons, menus, modals, messages éphémères, confirmations, erreurs, notifications. À utiliser pour créer ou modifier toute interaction visible sur Discord.
---

# Samae : expérience utilisateur sur Discord

Objectif : chaque interaction est compréhensible sans lire la documentation, rapide et cohérente avec
le reste du bot. Tout est en français, tutoiement évité dans les messages du bot (« vous »).

## Commandes slash

- Groupes par domaine (`/reunion`, `/projet`, `/suivi`, `/idee`…) ; sous-commandes au **verbe à
  l'infinitif** ou au nom court, en minuscules, avec tirets (`en-attente`). Pas plus de 25 par groupe.
- Description ≤ 100 caractères, commençant par « Admins : » pour une action d'administration.
- Paramètres en français sans accents dans le nom (`cle`, `onglet`), décrits avec `@app_commands.describe` ;
  `choices` pour une liste fermée, `autocomplete` pour une liste longue ; facultatifs en dernier.
- Chaque commande a sa `HelpEntry` (description, exemple réel) dans `src/services/help.py`.

## Réponses

- **Éphémère par défaut** (`ephemeral=True`) ; public seulement si toute l'équipe doit voir le résultat
  (annonce, compte rendu de réunion).
- Répondre en moins de 3 secondes : `await interaction.response.defer(ephemeral=True, thinking=True)`
  avant tout appel réseau, lecture de fichier ou calcul long ; puis `interaction.followup.send`.
  Travail bloquant (openpyxl…) dans `asyncio.to_thread`.
- Embeds via `src/discord_bot/embeds.py` : `make_embed(titre, description, footer=…)`, `add_field`,
  `success_embed`, `error_embed`. Couleurs et icônes : `src/services/design.py` (`Tone`, `Color`).
- Limites Discord centralisées dans `src/services/text_limits.py` (`truncate`, `chunk_lines`,
  `FIELD_VALUE`, `EMBED_DESCRIPTION`) ; longues listes en pages avec `PagesView` ; rapport complet en
  fichier joint (`discord.File`, .md).
- Texte venant des utilisateurs ou de GitHub/GitLab : `escape_markdown` ou `clean_text` ; jamais de
  mention qui notifie (`allowed_mentions=discord.AllowedMentions.none()`).

## Erreurs

- `send_error(interaction, message, title=…, tone="warning")` : dit **ce qui ne va pas et quoi faire**
  (« Envoyez un fichier Excel .xlsx. », « indiquez GITHUB_SYNC_REPOSITORY dans .env »).
- Les exceptions métier (`MeetingError`, `TrackerError`, `GitHubSyncError`…) portent un message
  français destiné à l'utilisateur ; une exception inattendue est journalisée (`log.exception`) et
  l'utilisateur reçoit un message générique, jamais une trace.

## Actions sensibles

- Confirmation obligatoire (`ConfirmView`) pour toute écriture importante : import, validation d'une
  proposition, décision, refonte du serveur. L'aperçu dit ce qui va changer et ce qui ne changera pas.
- Vérification côté bot en plus des permissions Discord : `@admin_only()` ; les droits sont décidés
  dans le service (`Actor.is_admin`), jamais seulement par l'interface.
- Boutons persistants (survivent au redémarrage) : `discord.ui.DynamicItem` avec un `custom_id` stable.

## Composants

- Bouton : libellé = action (« Valider », « Annuler »), style danger pour une suppression ; un seul
  bouton principal par message.
- Menu déroulant : > 5 choix fermés ; modal : saisie de texte libre (formulaire d'idée), libellés
  courts, champs facultatifs indiqués.
- Notifications automatiques (webhooks) : un embed par événement, regroupement plutôt que rafale,
  aucune mention.

## Vérifier

Test avec les faux Discord de `tests/test_meeting_commands.py` (`FakeInteraction`) : réponse éphémère,
texte attendu (`text_of`), refus pour un non-admin ; puis ligne dans `docs/test-manuel.md` pour le test
sur un vrai serveur (affichage téléphone compris).
