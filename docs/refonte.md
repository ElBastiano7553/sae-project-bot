# Refonte du serveur (`/refonte`, version 1.3.0)

`/refonte` transforme la structure existante du serveur pour la rendre conforme à
`config/server.toml` : catégories, salons, rôles, couleurs, emojis, permissions et ordre
d'affichage. Elle réutilise, renomme et déplace ce qui existe avant de créer ce qui
manque, garde les éléments personnalisés et ne supprime rien sans confirmation séparée.

> **Validation** : tout ce qui est décrit ici est vérifié par des tests sur un **faux
> serveur Discord en mémoire**. Rien n'a encore été essayé sur un vrai serveur Discord.
> Suivez [test-manuel.md](test-manuel.md) sur un serveur de test avant la production.

## 1. Audit de la version 1.2.0 (causes concrètes)

| Problème constaté | Cause dans le code de la 1.2.0 |
|---|---|
| Aucun emoji | Les noms de `server.toml` n'en contenaient pas et la politique « sobre » de la 1.2.0 les retirait ; aucune notion de style. |
| Rôles sans couleur | `/refonte roles:True` créait les rôles avec `Permissions.none()`, sans couleur ni ordre ; un rôle existant n'était jamais complété. |
| Salons manquants | Les salons facultatifs étaient ignorés sans `facultatifs:True` ; seuls les types texte et vocal existaient (ni annonces, ni forum, ni médias, ni conférence). |
| Pas de vraie réorganisation | Le plan partait bien d'une cible complète, mais n'agissait que sur les éléments reconnus : permissions, sujets et positions existants n'étaient jamais touchés ; les catégories existantes n'étaient pas réordonnées. |
| Éléments inconnus | Simplement listés, sans distinguer personnalisés, doublons et inconnus. |
| Retour arrière | Aucune commande : procédure manuelle d'après le journal. |
| `/setup` | Lit la même configuration (noms et alias) : il ne crée, ne renomme et ne supprime rien. Ce n'a pas changé. |

Limites Discord prises en compte : 100 caractères par nom (vérifié au chargement de la
configuration), 500 salons et 250 rôles (point bloquant), 50 salons par catégorie (signalé dans les risques),
hiérarchie des rôles (Samae ne modifie que les rôles placés sous le sien), types
annonces, forum, médias et conférence réservés aux serveurs **Communauté**.

## 2. Architecture

| Fichier | Rôle |
|---|---|
| `config/server.toml` | Cible complète : profils, styles, niveaux d'accès, catégories, salons, rôles, politique `[refonte]`. |
| `src/config.py` | Lecture et validation (emojis, couleurs `#RRGGBB`, permissions connues, jamais `administrator`, permissions sensibles réservées au staff, noms ≤ 100 caractères). |
| `src/services/redesign_target.py` | Cible pour un profil et un style : noms affichés, accès hérité de la catégorie, règles de permissions par cible. |
| `src/services/server_redesign.py` | Moteur de plan **pur** (aucun appel Discord) : inventaire, comparaison, opérations, aperçu, rapport. |
| `src/services/redesign_rollback.py` | Plan de retour arrière à partir d'un journal. |
| `src/discord_bot/snapshot.py` | Lecture seule du serveur (identifiants, types, positions, sujets, permissions, couleurs, hiérarchie). |
| `src/discord_bot/redesign.py` | Exécuteur : une opération à la fois, revérification avant chaque suppression, état d'avant noté pour le retour arrière. |
| `src/discord_bot/redesign_ui.py` | Aperçu paginé, boutons, menus de profil et de style. |
| `src/discord_bot/cogs/redesign.py` | Commandes `/refonte lancer`, `/refonte retour`, `/refonte journal` ; sauvegarde, journal, vérification finale. |

Aucune migration de base : les associations de catégories et de rôles sont stockées
dans la table clé/valeur existante (`category_bindings`, `role_bindings`).

## 3. Commandes

| Commande | Effet |
|---|---|
| `/refonte lancer [profil] [style] [facultatifs] [roles] [permissions] [suppressions]` | Analyse, aperçu, application sur confirmation. Par défaut : profil et style de `[refonte]`, rôles et permissions gérés, facultatifs et suppressions désactivés. |
| `/refonte retour [journal]` | Prépare le retour arrière d'une refonte (la plus récente par défaut), avec aperçu et confirmation. |
| `/refonte journal` | Dernières refontes, suppressions et retours : état, réussites, échecs, auteur. |

Déroulé de `/refonte lancer` :

1. **Analyse** (aucune modification) : inventaire du serveur, profil et style
   choisissables dans deux menus, résumé du plan.
2. **Prévisualiser** : plan paginé. Il présente, dans l'ordre :
   - la structure cible complète ;
   - les rôles avec leurs couleurs ;
   - les catégories et salons à créer, renommer ou modifier, puis à déplacer ;
   - les permissions à corriger ;
   - l'ordre ;
   - les associations de Samae ;
   - les risques ;
   - ce qui est impossible ou à faire à la main ;
   - les éléments personnalisés, les doublons et ceux de fonction inconnue ;
   - les éléments ignorés ;
   - **en dernier et à part**, les suppressions proposées.
3. **Appliquer les changements**, puis **Confirmer l'application**. Le plan est
   recalculé : si le serveur a changé depuis l'aperçu, rien n'est appliqué et les
   différences sont affichées.
4. **Résultat** : réussies, échouées (avec la raison), ignorées, étapes manuelles,
   vérification finale (nouvelle analyse après application : ce qui reste à faire),
   destinations de Samae.
5. Si `suppressions:True` : **Examiner les suppressions**, puis **Supprimer ces
   éléments** et **Confirmer les suppressions**. Cette liste est revérifiée juste
   avant de supprimer.

Les boutons ne répondent qu'à la personne qui a lancé la commande. Ses droits sont
revérifiés à chaque confirmation, et une seule refonte peut tourner à la fois. Un
double clic n'applique rien deux fois. Les boutons expirent après 10 minutes et le
message dit alors si des changements avaient déjà été appliqués.

## 4. Ce que fait la refonte, élément par élément

| Situation | Action |
|---|---|
| Salon ou catégorie reconnu (par identifiant enregistré, nom, nom stylé ou ancien nom) | **Conservé** (même identifiant, même historique), renommé au style choisi, déplacé dans la bonne catégorie, permissions et position corrigées. |
| Salon sans sujet | Sujet de la cible ajouté. Un sujet personnalisé est **conservé**. |
| Salon textuel attendu en annonces | Converti seulement sur un serveur Communauté ; sinon étape manuelle. |
| Élément absent | Créé avec ses permissions (forum, médias, conférence remplacés par texte ou vocal hors Communauté). |
| Rôle reconnu (identifiant, nom, ancien nom) | Réutilisé : nom, couleur, affichage séparé, mention et permissions **manquantes** complétés ; aucune permission n'est jamais retirée. |
| Rôle au-dessus de Samae ou de vous | Laissé tel quel, étape manuelle. |
| Nom listé dans `keep` | **Personnalisé** : conservé tel quel. |
| Homonyme d'un élément de la cible | **Doublon** : conservé et signalé ; suppression proposée seulement s'il est textuel et **vide**. |
| Autre élément hors cible | **Fonction inconnue** : conservé, signalé, rangé après les éléments de la cible. |
| Catégorie hors cible vidée par les déplacements | Suppression **proposée**. |
| Nom listé dans `obsolete` | Suppression **proposée** (avec avertissement s'il contient des messages). |
| Plusieurs salons pour une même fonction | Aucun n'est choisi au hasard : étape manuelle. |

Une suppression n'est jamais déduite de l'absence d'un élément dans la cible.

Ordre d'application :

1. sauvegarde de la base ;
2. journal ;
3. rôles, puis leur hiérarchie ;
4. catégories ;
5. salons, avec leurs déplacements ;
6. permissions ;
7. ordre des catégories et des salons ;
8. associations de Samae ;
9. vérification.

Une erreur n'arrête pas les autres opérations. Un salon dont la catégorie n'a pas pu
être créée est ignoré, jamais créé ailleurs. Un délai dépassé (60 s) compte comme un
échec, pas comme un succès. Relancer la refonte reprend exactement ce qui manque, et
une seconde refonte sur un serveur conforme ne fait rien.

## 5. Matrice des permissions

Seules les cibles gérées sont modifiées : @everyone, Samae, le rôle `ADMIN_ROLE_NAME` et
les rôles de la cible. Les autres permissions particulières (un rôle « Invité », un
membre…) sont conservées. Un salon sans niveau d'accès hérite de celui de sa catégorie.

| Niveau | @everyone | Samae | Staff (Responsable projet, Modérateur, admin) |
|---|---|---|---|
| Public | aucune surcharge | permissions du serveur | — |
| Lecture seule (annonces, bienvenue, décisions, comptes rendus, activité Git…) | ne peut pas écrire ni créer de fils | voir, écrire, intégrer, historique | peut écrire |
| Votes | réactions seulement | voir, écrire, réagir | Responsable projet peut écrire |
| Staff (ADMINISTRATION, bot-logs) | ne voit pas | voir, écrire, intégrer, historique | voit et écrit |

Rôles du profil « Projet d'équipe » (du plus haut au plus bas) :

| Rôle | Couleur | Permissions |
|---|---|---|
| Responsable projet | #C9A227 | gérer les messages, les fils et les événements ; épingler |
| Développement | #4F8FD6 | aucune |
| Game Design | #B07CD8 | aucune |
| Art & Design | #E07A9B | aucune |
| Testeur | #D19A4B | aucune |

Les rôles facultatifs (avec `facultatifs:True`) sont Responsable de réunion et les
rôles de notification auto-attribuables Notif · Annonces, Notif · Réunions et
Notif · Votes, prévus pour les futurs systèmes. Le profil « Communauté » ajoute
Modérateur, Membre et Nouveau membre.

Garde-fous :

- `administrator` est refusé dans la configuration ;
- les permissions sensibles (gérer les messages, expulser…) sont réservées aux rôles
  de type staff ;
- Samae n'accorde que ce qu'il possède lui-même ;
- rien n'est fait que vous ne pourriez pas faire vous-même ;
- un plan qui masquerait tous les salons textuels à @everyone est bloqué ;
- le propriétaire et les administrateurs Discord gardent toujours l'accès.

Ce qui est impossible est listé à part, jamais présenté comme réussi.

## 6. Configuration (`config/server.toml`)

- `[refonte]` : profil et style par défaut ; `permissions`, `positions` et `edit_roles`
  pour désactiver une partie du travail ; `deletions_allowed` pour interdire toute
  suppression ; `keep` pour les éléments personnalisés ; `obsolete` pour les éléments
  à proposer à la suppression.
- `[profiles.<clé>]` : `label`, `description`, `style`. Un salon ou un rôle avec
  `profiles = [...]` n'existe que dans ces profils.
- `[styles.<clé>]` : modèles `category`, `channel` et `voice` avec `{emoji}` et `{nom}`.
  Les styles disponibles sont projet, sobre, professionnel, communautaire et gaming.
- `[access.<clé>]` : `label` et `rules."<cible>"` avec `allow` et `deny`.
- `[categories.<clé>]` : `name`, `emoji`, `aliases`, `access`, `private`.
- `[channels.<clé>]` : `name`, `category`, `type`, `emoji`, `topic`, `access`,
  `aliases`, `optional`, `profiles`. Le type vaut `text`, `announcement`, `voice`,
  `stage`, `forum` ou `media`.
- `[roles.<clé>]` : `name`, `color`, `kind`, `permissions`, `hoist`, `mentionable`,
  `self_assignable`, `optional`, `aliases`, `description`, `profiles`. Le `kind` vaut
  `staff`, `responsabilite`, `fonction`, `membre` ou `notification`.

Les emojis sont des emojis Unicode, donc disponibles partout. Ne changez pas une clé :
c'est la fonction de l'élément pour Samae. Une erreur de configuration est expliquée
dans Discord et rien n'est appliqué.

## 7. Permissions nécessaires

| Qui | Permissions |
|---|---|
| Samae | **Gérer les salons** (indispensable). **Gérer les rôles** pour les rôles et les permissions des salons ; son rôle doit être **au-dessus** des rôles à gérer. Il doit aussi avoir lui-même chaque permission qu'il pose (voir, écrire, intégrer des liens, historique, réactions, gérer les messages, les fils et les événements, épingler). Pas besoin d'Administrateur. |
| Vous | Admin du bot (Gérer le serveur, Administrateur ou rôle `ADMIN_ROLE_NAME`) **et** Gérer les salons. Gérer les rôles pour toucher aux rôles et aux permissions. |

## 8. Sauvegarde, journal et retour arrière

- **Avant** chaque application : sauvegarde vérifiée de la base dans `data/backups/`
  (nom contenant `avantrefonte`, `avantsuppressions` ou `avantretour`). Si la
  sauvegarde échoue, rien n'est appliqué.
- **Journal** : `data/refonte/<type>-AAAAMMJJ-HHMMSS.json`, réécrit après chaque
  opération. Il contient l'auteur, la date, l'empreinte du plan, l'état complet d'avant
  (identifiants, noms, types, catégories, positions, sujets, couleurs, hiérarchie,
  permissions) et les associations de Samae. Pour chaque opération, il garde son
  résultat et l'état relu juste avant de la faire. Son état final vaut `terminee`,
  `partielle` ou `interrompue`. Aucun secret n'y figure.
- **`/refonte retour`** lit le journal et propose, avec aperçu et confirmation :
  - de rétablir noms, sujets et types ;
  - de remettre les salons dans leur ancienne catégorie ;
  - de rétablir les permissions des cibles modifiées ;
  - de rendre aux rôles leur nom, leur couleur et leurs permissions (celles ajoutées
    sont retirées) ;
  - de rétablir les positions et les associations de Samae.

  Les éléments **créés** par la refonte sont proposés à la suppression dans une liste
  séparée, seulement s'ils sont vides. Les éléments supprimés sont recréés **vides**.
- Ce qui a changé à la main depuis la refonte n'est pas touché et est signalé.

Limites du retour arrière, affichées dans l'aperçu :

- les messages d'un salon supprimé sont perdus définitivement ;
- un rôle recréé n'est réattribué à personne ;
- les positions sont rétablies au mieux ;
- un salon créé par la refonte qui contient des messages est conservé.

Le retour arrière n'est donc **pas** une restauration complète. Pour la configuration
du bot seule, vous pouvez aussi arrêter Samae et remplacer `data/sae_bot.sqlite3` par
la sauvegarde `avantrefonte`.

Chaque refonte, suppression ou retour apparaît dans `/admin history`.

## 9. Après une refonte

- Lancez `/refresh` pour que les messages permanents citent les nouveaux noms.
- Mettez `ALERTS_CHANNEL=logs` dans `.env` pour envoyer les alertes dans `#bot-logs`,
  puis redémarrez Samae.
- Annonces, forums, médias et conférences demandent la fonction Communauté
  (Paramètres du serveur > Activer la Communauté), puis une nouvelle refonte.
- Les messages écrits à la main peuvent encore citer les anciens noms.

## 10. Tests

| Fichier | Contenu |
|---|---|
| `tests/test_server_redesign.py` | 26 tests sur le plan et l'exécuteur avec le faux serveur `tests/fake_guild.py`. |
| `tests/test_redesign_command.py` | 31 tests de la commande complète. |
| `tests/test_config.py` | Validation de la configuration, avec 12 configurations invalides. |

Le plan et l'exécuteur couvrent :

- serveur vide ;
- serveur déjà structuré ;
- éléments personnalisés, doublons et rôles existants ;
- permissions manquantes, hiérarchie incompatible, utilisateur sans « Gérer les rôles » ;
- erreur Discord et délai dépassé ;
- seconde exécution ;
- suppressions jamais déduites ;
- retour arrière et ses limites ;
- styles, profils, Communauté, verrouillage.

La commande complète couvre :

- accès refusé, droits revérifiés, autre utilisateur ;
- aperçu périmé et double clic ;
- échec partiel et interruption ;
- suppressions confirmées ou non, liste modifiée entre-temps ;
- retour arrière ;
- réunions préservées ;
- sauvegarde et journal.

Suite complète : `python -m pytest -q` (730 tests).

Ces tests simulent Discord : la lecture du vrai serveur (`snapshot.py`) et les appels
réels (positions groupées, conversion en annonces, limites de débit) restent à
valider sur un serveur de test.
