# Réunions (Samae 2.0, lot 1)

Le groupe de commandes `/reunion` organise une réunion de bout en bout : ordre du
jour, participants, passage d'une étape à l'autre, décisions, réserves, actions à
faire et compte rendu final. Toutes les données sont dans SQLite (migration v7) :
une réunion survit à un redémarrage du bot, et une réunion terminée garde son
historique et son compte rendu.

## Déroulement type

1. `/reunion creer titre:Point sprint 3 etapes:Bilan ; Démo ; Planning participants:@Bob @Chloé duree:45`
   publie le **panneau** de la réunion dans le salon courant. Les étapes sont
   séparées par « ; ». Les participants se mentionnent avec `@` (Discord propose
   les membres pendant la saisie).
2. Les autres membres cliquent sur **Rejoindre** (ou `/reunion rejoindre`).
3. Le responsable clique sur **Démarrer** : la première étape passe « en cours ».
4. Pendant la réunion :
   - **Étape suivante** / **Étape précédente** (responsable) ;
   - `/reunion decision texte:… motif:…` (responsable) ;
   - `/reunion reserve texte:… type:Désaccord` (participants) ;
   - `/reunion action description:… responsable:@Bob echeance:05/11` (participants) ;
   - `/reunion point texte:…` ajoute un point à la fin de l'ordre du jour.
5. **Terminer** (après confirmation) : l'étape en cours est marquée traitée, le
   compte rendu est **figé** puis publié dans `#comptes-rendus` (créé par `/refonte`),
   à défaut dans le salon des décisions (`#decisions-et-comptes-rendus` avant la refonte),
   et si aucun des deux n'est associé, dans le salon du panneau.

Une décision, une réserve ou une action est rattachée automatiquement à l'étape en
cours au moment où elle est enregistrée.

## Qui peut faire quoi

| Action | Qui |
|---|---|
| Consulter (`voir`, `liste`, `progression`, `compte-rendu`) | tous les membres |
| Rejoindre, quitter | tous les membres (le responsable ne peut pas quitter sa réunion) |
| Ajouter un point, une réserve, un désaccord, une action | participants, responsable, admins du bot |
| Démarrer, changer d'étape, enregistrer une décision, inviter, terminer, annuler, déplacer le panneau, republier le compte rendu | **responsable** de la réunion ou **admin du bot** |

« Admin du bot » : permission « Gérer le serveur » ou « Administrateur », ou rôle
`ADMIN_ROLE_NAME` (même règle que les autres commandes d'administration).

Ces droits sont vérifiés **par le bot à chaque commande et à chaque clic**, dans
`src/services/meetings.py` : afficher ou masquer un bouton n'est qu'un confort.

## États d'une réunion

| État | Couleur du panneau | Possible ensuite |
|---|---|---|
| prévue | violet | démarrer, annuler |
| en cours | rouge carmin | étapes, notes, actions, terminer, annuler |
| terminée | violet sombre | consultation seulement |
| annulée | gris | consultation seulement |

Une commande impossible dans l'état actuel (décision avant le démarrage, étape
suivante après la fin…) est refusée avec une explication, sans rien modifier.

## Le panneau

- Un seul message par réunion, **modifié sur place** à chaque action.
- S'il est supprimé alors que la réunion est ouverte, il est republié **une fois**
  à la prochaine action, et le nouveau message remplace l'ancien en base.
- `/reunion voir publier:True` le déplace dans le salon courant : le nouveau panneau
  est publié avant que l'ancien soit supprimé.
- Les boutons fonctionnent après un redémarrage : leur identifiant
  (`samae:mtg:<réunion>:<action>:<révision>`) suffit à retrouver la réunion.
- **Panneau périmé** : chaque modification augmente la *révision* de la réunion.
  Un clic sur « Étape suivante » depuis un panneau qui n'est plus à jour (double
  clic, deux personnes en même temps) est refusé, et le panneau est redessiné. On
  ne saute donc jamais deux étapes par erreur.
- Le panneau montre les 4 dernières décisions, réserves et actions ; le compte rendu
  contient tout.

## Le compte rendu

Contenu : titre et date, statut, responsable, durée effective et prévue,
participants, objectifs, étapes (traitées, en cours, non traitées), décisions avec
leur motif et leur auteur, réserves et désaccords, actions avec responsable et
échéance. Les résultats de classements seront ajoutés au lot 2 (les classements
n'existent pas encore).

- `/reunion compte-rendu` : lecture privée, paginée (boutons **Précédent** / **Suivant**) ; **aperçu
  provisoire** tant que la réunion n'est pas terminée.
- À la clôture, le compte rendu est figé dans la base (colonne `meetings.report`,
  dans la même transaction que la clôture) : il ne change plus ensuite.
- Publication : une page par message, au plus 10 messages ; au-delà, la suite reste
  lisible avec `/reunion compte-rendu`. `publier:True` republie un compte rendu
  supprimé, sans doublon s'il existe encore.
- Les mentions affichées (`@Alice`) ne notifient personne : le bot désactive toutes
  les mentions.

## Limites

| Élément | Limite |
|---|---|
| Titre · point de l'ordre du jour | 100 caractères |
| Description · objectifs | 1000 caractères |
| Décision, réserve | 500 caractères (motif : 300) |
| Action | 300 caractères |
| Étapes · participants | 25 · 25 |
| Notes · actions par réunion | 200 · 50 |
| Durée prévue | 5 à 600 minutes |
| Créations | 5 réunions par membre et par heure (vérifié en base ; pas de limite pour les admins) |
| Modifications | 30 par membre et par minute (en mémoire) |

Un texte trop long est **refusé** avec un message clair plutôt que coupé en silence.

## Données (migration v7)

| Table | Contenu |
|---|---|
| `meetings` | titre, description, objectifs, responsable, statut (contrainte `CHECK`), durée prévue, salon et message du panneau, compte rendu figé (JSON), messages du compte rendu, `revision`, créateur, dates de création, de début, de fin et de modification |
| `meeting_participants` | réunion, membre, nom affiché, date d'arrivée (clé primaire : réunion + membre, donc pas de doublon) |
| `meeting_steps` | position, intitulé, statut (`pending`, `current`, `done`), dates ; index unique : **une seule étape en cours** par réunion |
| `meeting_notes` | décisions, réserves et désaccords : texte, motif, étape, auteur, date |
| `meeting_actions` | description, responsable, échéance, statut, auteur, date |
| `meeting_log` | historique : qui a fait quoi et quand (création, démarrage, étapes, notes, clôture…) |

Chaque modification est une **transaction unique** : vérification de la révision,
écritures, journal. Une erreur au milieu annule tout. La migration v7 ne fait que
créer des tables : aucune donnée existante n'est modifiée, et une sauvegarde
vérifiée de la base est faite avant la migration.

## Ce que le lot 1 ne fait pas encore

- Associer des idées à une réunion et y lancer un classement : lot 2.
- Marquer une action comme faite après la réunion, transférer la responsabilité
  d'une réunion : pas encore prévu (à demander si utile).
- Rappels automatiques avant une réunion : non prévu (aucune tâche planifiée n'est
  ajoutée).

## Aide intégrée et apparence (1.2.0)

- `/reunion help [sujet]` ouvre une aide en 6 pages (créer, participer, animer,
  décisions et réserves, terminer, qui peut faire quoi), visible seulement par vous.
- Une **décision officielle** est réservée au responsable de la réunion et aux admins.
  Proposer une décision pour la faire valider par l'équipe n'existe **pas encore**.
- Couleurs du panneau : gris-bleu (prévue), bleu clair (en cours), vert (terminée),
  gris (annulée). Les boutons portent un libellé explicite, sans emoji
  (« Annuler la réunion » plutôt qu'une croix). Les identifiants des boutons n'ont pas
  changé : les panneaux déjà publiés continuent de fonctionner.
