# Gestionnaire de réunions : idées, tier lists, débats (1.14.0)

Ce module complète `/reunion` (ordre du jour, décisions, actions) avec la préparation collective des
choix de jeu : collecte d'idées, classement S à F à votes masqués, débats sur les idées qui divisent,
puis compte rendu. Toute la logique est dans `src/services/ideas.py` (sans discord.py) ; le SQL dans
`src/database/ideas.py` (migration v15, créations de tables uniquement) ; les commandes dans
`src/discord_bot/cogs/ideas.py`.

## Parcours type

1. Chacun propose ses idées : `/idee proposer` ouvre un **formulaire Discord** (nom, style de jeu,
   genre de gameplay, description facultative). **Cinq idées en attente au plus par membre**, vérifié
   dans la transaction d'écriture (deux clics simultanés ne donnent pas six idées).
2. Samae signale les **idées proches** déjà au catalogue (mots communs du nom, du style, du genre et de
   la description, sans accents ni mots vides ; même genre + même style = proches). On les regroupe
   avec `/idee regrouper` ; `/idee catalogue` affiche les familles, triées par genre.
3. Un administrateur crée la tier list : `/tierlist creer titre:… [idees:3, 5, 8] [reunion:2]`
   (toutes les idées en attente par défaut, 25 au plus).
4. Chacun vote : `/tierlist voter` ouvre un **bulletin éphémère** (un menu S à F par idée, 4 par page).
   On peut changer d'avis jusqu'à « Valider mon bulletin » ; ensuite le bulletin est verrouillé.
   `/tierlist progression` montre qui a validé, **jamais aucun vote**.
5. L'administrateur clôture : `/tierlist cloturer` (confirmation) publie les résultats **agrégés** :
   rang, moyenne, médiane, écart type, répartition, égalités, avis extrêmes **anonymes**, désaccords.
   Seuls les bulletins validés comptent (les autres sont signalés).
6. `/debat controverses tierlist:N` liste les idées controversées ou à égalité ; `/debat ouvrir`
   crée un débat. Chacun ajoute des **arguments pour, contre ou des objections** (`/debat argument`).
7. Si besoin, un administrateur lance un **vote de départage** (`/debat departage options:A ; B ; C`,
   2 à 5 options, un vote par membre modifiable, résultats masqués pendant le vote).
8. Un membre **propose** la décision (`/debat proposer`, avec facultativement « retenir » ou
   « écarter » l'idée) ; seul un administrateur la **confirme** (`/debat confirmer` puis bouton).
   Si la proposition a changé entre l'affichage et le clic, la confirmation est refusée.
9. Une décision confirmée est publiée dans le salon, change le statut de l'idée si demandé et est
   ajoutée aux DECISIONS du suivi Excel (si le classeur est importé) comme décision **déjà validée**.
10. Le **compte rendu** de la réunion (`/reunion terminer`) contient en plus : tier lists rattachées
    (classement), idées retenues, idées écartées, décisions issues des débats et questions ouvertes
    (débats non tranchés), à côté des décisions, réserves et actions de la réunion.

## Échelle et calculs

| Rang | S | A | B | C | D | F |
|---|---|---|---|---|---|---|
| Points | 6 | 5 | 4 | 3 | 2 | 1 |

- **Moyenne** arrondie au rang le plus proche (5,5 → S). Classement par moyenne puis médiane ;
  même moyenne = même rang (**égalité**, listée à part).
- **Avis extrême** : un vote à 3 points ou plus de la moyenne des autres votes (3 votants au moins).
  Le rapport dit « un F alors que la moyenne des autres est 5,3 », sans nommer personne.
- **Désaccord** : écart d'au moins 3 rangs entre le vote le plus haut et le plus bas, ou écart type ≥ 1,5.
- **Une idée classée F n'est jamais éliminée** : elle est signalée « à rediscuter » ; seul un
  administrateur la déclare écartée (`/idee statut`) ou une décision de débat confirmée.

## Permissions

| Action | Qui |
|---|---|
| Proposer, voir, chercher, voter, argumenter, proposer une décision | tout membre |
| Modifier ou retirer une idée | son auteur ou un administrateur (idée en attente) |
| Regrouper deux idées | l'auteur de l'une des deux ou un administrateur |
| Statut d'une idée, créer/clôturer une tier list, départage, confirmer une décision | administrateurs du bot |
| Annuler un débat | son auteur ou un administrateur |

Les données sont isolées par serveur Discord : une idée d'un autre serveur répond « n'existe pas ».

## Tables (migration v15)

`ideas`, `idea_history` (chaque proposition, modification, statut, regroupement), `tierlists`,
`tierlist_items`, `tierlist_votes` (unique par votant et idée), `tierlist_voters` (validation),
`debates`, `debate_entries`, `debate_options`, `debate_votes` (unique par votant). Les résultats d'une
tier list sont figés en JSON à la clôture (`tierlists.results`).
