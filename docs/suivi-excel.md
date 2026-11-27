# Suivi du projet : le classeur « GAME DESIGN DATABASE — LUNASTEH » dans Samae (1.14.0)

## État au 27/11/2026

Le classeur **n'était pas accessible** depuis l'environnement de travail (ni pièce jointe, ni fichier
du projet, ni PC joignable). Rien n'a donc été inventé à sa place : le système est construit pour
**découvrir lui-même** la structure du vrai classeur (onglets, ligne d'en-tête, colonnes, formules,
identifiants, statuts, listes déroulantes) et il a été testé sur un classeur de **test** de même
forme (`tests/tracker_fixture.py`, clairement marqué comme tel).

Pour aller plus loin, il faut l'une de ces deux actions (voir « Mise en route ») :

1. lancer `python -m src.tracker.inspect "GAME DESIGN DATABASE — LUNASTEH.xlsx"` sur le PC, qui
   produit l'analyse complète **sans modifier le fichier** ;
2. ou importer le fichier dans Discord avec `/suivi importer` (aperçu avant toute écriture).

## Choix de l'architecture : une seule source de vérité

| Option | Avantages | Problèmes | Verdict |
|---|---|---|---|
| **Excel direct** (le bot écrit dans le .xlsx) | rien à importer | fichier verrouillé quand il est ouvert dans Excel ; conflits si quelqu'un l'édite en même temps ; openpyxl perd graphiques et images à l'enregistrement ; risque d'écraser l'original | écarté |
| **Synchronisation bidirectionnelle** | chacun garde son outil | deux sources de vérité, conflits impossibles à trancher automatiquement, risque de perte | écarté |
| **Base centralisée (SQLite de Samae) + import/export** | une seule source de vérité, transactions, historique, sauvegardes déjà en place, validation humaine possible | il faut importer une fois, puis exporter pour avoir un .xlsx à jour | **retenu** |

Après l'import, **la base de Samae fait référence**. Le classeur n'est jamais modifié : on obtient un
.xlsx à jour par `/suivi exporter`, qui écrit une **nouvelle copie** à partir de l'original.

## Ce que fait chaque étape

- **Analyse** (`src/tracker/workbook.py`, `inspect_workbook`) : pour chaque onglet, ligne d'en-tête
  (la plus remplie des 15 premières lignes), colonnes, nombre de valeurs, colonnes à formule (avec un
  exemple), colonne d'identifiant (« ID », « Réf », « Code »…), de titre, de statut, valeurs de statut
  observées et **listes déroulantes** (validation de données), motif des identifiants (« F-### »),
  doublons, références entre onglets (le DASHBOARD qui cite FEATURES…), plages fusionnées, tableaux,
  graphiques et images.
- **Import** (`/suivi importer`, administrateurs) : aperçu (lignes nouvelles, modifiées, identiques,
  absentes du fichier, gardées), **sauvegarde de la base** (`backups/suivi/`), copie de l'original
  dans `data/tracker/originals/<empreinte>.xlsx` (jamais réécrite), puis écriture. **Rien n'est
  supprimé** : une ligne absente du nouveau fichier reste dans Samae. Une ligne modifiée par Samae
  depuis le dernier import **garde la version de Samae** (la base fait référence).
- **Formules** : pour les colonnes calculées, Samae lit le résultat mis en cache par Excel et ne les
  modifie **jamais** (une proposition sur une colonne à formule est refusée).
- **Consultation** (tout membre) : `/suivi progression`, `chercher`, `fonctionnalites` (restantes par
  défaut), `decisions`, `idees`, `questions`, `voir`.
- **Modifications** :
  - ajout dans **IDEAS** ou **OPEN_QUESTIONS** : direct (peu risqué), historisé ;
  - tout le reste (statut d'une ligne, ajout d'une décision, d'une fonctionnalité…) : **proposition
    en attente** ; un administrateur l'applique avec `/suivi valider` (bouton de confirmation,
    sauvegarde avant) ou la refuse ;
  - une **décision officielle n'est jamais ajoutée ou modifiée sans validation humaine**, même
    proposée par un administrateur ; seule exception, une décision de débat déjà confirmée par un
    administrateur (`/debat confirmer`) est enregistrée comme validée ;
  - un statut doit appartenir à la liste déroulante de la colonne (ou aux valeurs déjà présentes) ;
  - si la ligne a changé entre la proposition et la validation, la validation est refusée.
- **Identifiants** : une nouvelle ligne reçoit l'identifiant suivant selon le motif détecté
  (« F-003 » → « F-004 ») ; sans motif, « SAMAE-1 », « SAMAE-2 »…
- **Historique** : chaque proposition (auteur, date, valeur avant, valeur après, validateur) est
  conservée (`/suivi historique`) ; l'export ajoute ces modifications au **CHANGELOG** de la copie.
- **Export** (`/suivi exporter`, administrateurs) : nouvelle copie `data/tracker/exports/…-samae-<date>.xlsx`
  envoyée dans Discord. Seules les cellules de données modifiées ou ajoutées sont écrites ; les
  cellules à formule restent intactes ; les formules des nouvelles lignes sont recopiées de la ligne du
  dessus avec des références décalées ; Excel recalcule tout à l'ouverture (DASHBOARD compris).

### Limite connue

openpyxl ne recopie pas les **graphiques, images et tableaux croisés dynamiques** dans une copie.
L'analyse et l'export le signalent. L'original n'étant jamais modifié, il reste la référence visuelle
du DASHBOARD ; les formules du DASHBOARD, elles, sont conservées dans l'export.

## Mise en route (Romain)

1. Sur le PC (PowerShell, dans le dossier du dépôt, environnement virtuel activé) :
   `pip install -r requirements.txt` puis
   `python -m src.tracker.inspect "C:\chemin\GAME DESIGN DATABASE — LUNASTEH.xlsx" --markdown analyse.md`.
   Le rapport `analyse.md` montre ce que Samae a compris de chaque onglet. Il ne contient que la
   structure et quelques valeurs de statut, pas les lignes.
2. Si une colonne n'est pas reconnue (identifiant, titre ou statut), le plus simple est de renommer
   son en-tête dans Excel (« ID », « Titre », « Statut ») ; sinon, signalez-la pour ajuster les
   synonymes dans `src/tracker/workbook.py`.
3. Après le déploiement de la 1.14.0 : `/suivi importer` avec le fichier en pièce jointe, vérifier
   l'aperçu, confirmer. Puis `/suivi progression` et `/samae demande:Quelles fonctionnalités restent à développer ?`.

## Fichiers et sécurité

- Taille maximale : 10 Mio. Le fichier est lu en mémoire ; aucune macro n'est exécutée (openpyxl ne
  les exécute jamais).
- Dossiers : `data/tracker/originals/`, `data/tracker/exports/` (non suivis par Git), sauvegardes dans
  `<BACKUP_DIR>/suivi/` (30 gardées). Couverts par `ReadWritePaths` du service systemd.
- Tables : `tracker_imports`, `tracker_rows` (une ligne Excel = un JSON « en-tête → valeur »),
  `tracker_changes` (propositions et historique).
