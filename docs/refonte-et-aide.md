# Refonte du serveur, centre d'aide et identité visuelle (version 1.2.0)

Ce document décrit `/refonte`, `/help`, `/reunion help` et la nouvelle apparence de
Samae : ce qui a été audité, comment l'utiliser, comment revenir en arrière et ce qui
reste à faire à la main. Rien n'a été testé sur le vrai serveur Discord : les tests
utilisent un faux serveur en mémoire.

## 1. Audit avant la refonte (22/10/2026)

| Point | Constat |
|---|---|
| Version de départ | 1.1.0 (Samae 2.0, lot 1 : réunions), schéma SQLite v7, PR #5 et #6 fusionnées. |
| Tests de départ | 465 tests verts (`python -m pytest -q`). |
| Structure | `config/server.toml` décrivait 6 catégories numérotées (« 01 · ACCUEIL & ORGANISATION » … « 06 · VOCAL »). `/setup` ne faisait que **reconnaître** les salons : aucune création, aucun renommage. |
| Associations | Table `channel_bindings` (fonction → identifiant du salon). Les messages permanents, les panneaux et comptes rendus de réunion sont retrouvés par identifiant : renommer ou déplacer un salon ne les casse pas. |
| Réunions | Boutons persistants `samae:mtg:<id>:<action>:<révision>`, comptes rendus publiés dans `decisions`. Aucune proposition de décision soumise au vote : seul le responsable ou un admin enregistre une décision. |
| Aide | `/help` : un seul embed par catégorie, sans navigation. Pas d'aide dédiée aux réunions. |
| Apparence | Beaucoup d'emojis dans les titres, les champs et les boutons ; couleurs vives non homogènes. |
| Risques relevés | Renommer un salon change le texte des messages qui le citent par son nom ; le marqueur de pied de page des messages permanents (« Message géré par SAE Project Bot · réf. ») ne doit pas changer, sinon le bot ne retrouve plus ses messages ; la limite de 25 sous-commandes par groupe (`/reunion` en a maintenant 19). |

## 2. Structure cible

`config/server.toml` décrit maintenant la structure cible. Les anciens noms y sont
gardés comme **alias** : `/setup` et `/refonte` reconnaissent les deux.

| Catégorie | Salons (ancien nom entre parenthèses) |
|---|---|
| ACCUEIL | bienvenue, annonces, règles-et-fonctionnement* |
| ESPACE COMMUN | général (discussion-generale), présence-et-disponibilités*, ressources-et-liens* |
| CONCEPTION DU JEU | idées (idees-et-game-design), game-design (gameplay-et-ia), décisions (decisions-et-comptes-rendus), assets-et-audio, votes-et-tier-lists*, références-visuelles* |
| ORGANISATION | réunions, comptes-rendus, planning (planning-et-deadlines), suivi-des-actions* |
| DÉVELOPPEMENT | développement (developpement-python), réseau-multijoueur, bugs-et-problèmes (bugs-et-tests), code-review, tests-et-builds (builds-et-tests), gitlab-et-commits (gitlab-activite), bilan-ia |
| LIVRABLES SAE | documentation-et-preuves, préparation-revues |
| VOCAL | Réunion d'équipe, Travail en commun |
| ADMINISTRATION (privée) | bot-logs, configuration* |

`*` : salon facultatif, créé seulement avec `facultatifs:True`. Les salons existants
qui ne sont pas dans la cible sont **gardés tels quels** (section « Hors de la
structure cible » de l'aperçu).

Rôles proposés (créés seulement avec `roles:True`, **sans aucune permission**) :
Responsable projet, Game Design, Développement, Art & Design, Testeur. Aucun rôle
« Admin » ni « Membre » n'est créé : `ADMIN_ROLE_NAME` et @everyone jouent déjà ce rôle.

## 3. Utiliser `/refonte`

1. Un admin du bot qui a lui-même **Gérer les salons** tape `/refonte`
   (options : `facultatifs`, `roles`). Rien n'est modifié à ce stade.
2. **Analyse** : catégories, salons, rôles, permissions du bot, salons déjà associés.
3. **Prévisualiser** : le plan, page par page : catégories et salons à créer, à
   renommer, à déplacer, associations à enregistrer, risques, points bloquants,
   étapes manuelles. La dernière ligne rappelle « 0 suppression ».
4. **Appliquer la refonte**, puis **Confirmer l'application**. Le bot refait
   l'analyse : si le serveur a changé depuis l'aperçu, rien n'est appliqué et le
   nouveau plan est affiché.
5. Le bot sauvegarde la base, applique les opérations une par une (catégories, puis
   salons, puis rôles), enregistre les associations par identifiant, écrit un journal
   et affiche le rapport : réussies, échouées (avec la raison), ignorées, étapes
   manuelles, puis le diagnostic des permissions.
6. Lancez ensuite `/refresh` pour mettre à jour le texte des messages permanents
   (ils citent les salons par lien, donc avec leur nouveau nom).

Garanties vérifiées par les tests :

- aucune suppression (le faux serveur échoue si le bot tente de supprimer) ;
- seule la personne qui a lancé la commande peut cliquer ; ses droits sont revérifiés
  à la confirmation ; une seule refonte à la fois ;
- un échec n'arrête pas les autres opérations ; un salon dont la catégorie n'a pas pu
  être créée est ignoré ; un délai dépassé (60 s par opération) est signalé comme
  incertain ;
- une deuxième refonte sur un serveur déjà refait ne fait rien ;
- les réunions, leurs panneaux et leurs boutons continuent de fonctionner ;
- la catégorie ADMINISTRATION est masquée à @everyone et visible par le bot et le rôle
  `ADMIN_ROLE_NAME` ;
- si la sauvegarde est impossible, rien n'est appliqué.

Points bloquants (bouton **Appliquer** désactivé) : le bot n'a pas « Gérer les
salons » (ou « Gérer les rôles » quand `roles:True`), ou la limite de 500 salons
serait dépassée. L'aperçu expire après 10 minutes sans rien modifier.

## 4. Permissions

| Qui | Permissions |
|---|---|
| Bot | Gérer les salons ; Gérer les rôles si `roles:True` (son rôle doit être au-dessus des rôles créés). Pas besoin d'Administrateur. |
| Personne qui lance `/refonte` | Admin du bot (Gérer le serveur, Administrateur ou rôle `ADMIN_ROLE_NAME`) **et** Gérer les salons (et Gérer les rôles si `roles:True`). |

## 5. Sauvegarde, journal et retour en arrière

- Avant d'appliquer : sauvegarde vérifiée de la base dans `data/backups/`
  (nom contenant `avantrefonte`).
- Après : journal JSON dans `data/refonte/refonte-AAAAMMJJ-HHMMSS.json` : qui, quand,
  l'empreinte du plan, l'état d'avant (noms et identifiants), chaque opération et son
  résultat. Aucun secret n'y figure.
- Revenir en arrière : rien n'ayant été supprimé, il suffit de renommer ou redéplacer
  les salons d'après la colonne « avant » du journal, puis de lancer `/setup`. Pour
  restaurer aussi la configuration du bot, arrêtez-le, remplacez
  `data/sae_bot.sqlite3` par la sauvegarde `avantrefonte`, et relancez-le.
- Chaque refonte apparaît dans `/admin history`.

## 6. Étapes manuelles après la refonte

- Mettre `ALERTS_CHANNEL=logs` dans `.env` pour envoyer les alertes dans `#bot-logs`,
  puis redémarrer le bot.
- La catégorie « 04 · GITLAB AUTOMATISÉ » devient vide : la supprimer à la main si
  vous le souhaitez (le bot ne supprime jamais rien).
- Ordre des catégories : seules les catégories créées sont placées au bon endroit.
  Réordonnez les catégories existantes par glisser-déposer dans Discord.
- Permissions des catégories existantes : non modifiées. Si l'aperçu signale une
  catégorie privée visible par tout le monde, corrigez-la dans Discord.

## 7. Centre d'aide

- `/help [section]` : 6 pages (Accueil, Réunions, Commandes, Organisation,
  Administration, Dépannage), un menu de sections, Précédent / Accueil / Suivant et
  l'indicateur de page. Les sections d'administration n'apparaissent que pour les
  admins du bot.
- `/reunion help [sujet]` : 6 pages (Créer, Participer, Animer, Décisions et réserves,
  Terminer, Qui peut faire quoi).
- Pour ne pas devenir obsolète : la page « Commandes » est générée à partir des
  commandes enregistrées dans Discord, et des tests vérifient que chaque exemple
  correspond à une vraie commande avec de vraies options, qu'aucune page ne cite une
  commande inexistante, que les pages des membres ne citent pas de commande d'admin,
  et que les fonctions prévues (tier lists, sondages, proposition de décision…) sont
  présentées comme « Pas encore disponible ».
- Le texte des pages est dans `src/services/help.py`.

## 8. Identité visuelle

- Palette : fond `#20242B`, surface `#2B313B`, accent `#8294A8`, texte `#E6EAF0`
  (`Palette` dans `src/services/design.py`). Couleurs d'état atténuées : réussite,
  échec, avertissement, information (accent), fusion, neutre, en direct.
- Titres, champs et boutons sans emoji ; libellés explicites (« Annuler la réunion »,
  « Confirmer l'application »).
- Emojis gardés volontairement : marqueurs d'état dans les listes (✅ ❌ ⚠️ dans les
  diagnostics, les étapes de réunion, les pipelines GitLab), icônes des notifications
  GitLab et du tableau de bord, préfixe 📣 de l'annonce en cours (utilisé par le
  code). L'harmonisation de ces listes reste à faire.
- Le marqueur de pied de page des messages permanents n'a **pas** changé.

## 9. Tests et limites

- `tests/test_server_redesign.py` (18 tests) : plan pur, idempotence, alias, cas
  ambigus, limites, empreinte.
- `tests/test_redesign_command.py` (21 tests) : commande complète sur un faux serveur
  Discord (droits, aperçu, confirmation, échecs, délais, sauvegarde, journal,
  réunions préservées).
- `tests/test_help_center.py` (17 tests) : contenu, limites Discord, navigation.
- Suite complète : 521 tests verts.

Limites connues :

- `snapshot_guild` (lecture du vrai serveur) n'est pas testé contre de vrais objets
  discord.py ; un essai sur un serveur de test est recommandé avant la production.
- Les limites de débit de Discord sont gérées par discord.py ; une refonte complète
  (une quarantaine d'opérations) peut prendre une à deux minutes.
- Les catégories existantes ne sont pas réordonnées.

## 10. Lancer et synchroniser les commandes (Windows)

```powershell
.\.venv\Scripts\Activate.ps1
python -m pytest -q
python -m src.main
```

Au démarrage, le bot synchronise ses commandes sur le serveur `DISCORD_GUILD_ID` :
`/refonte`, `/help section:` et `/reunion help` apparaissent tout de suite. Si une
commande n'apparaît pas, redémarrez Discord (Ctrl+R).
