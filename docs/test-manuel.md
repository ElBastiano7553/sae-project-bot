# Plan de test manuel sur un serveur Discord de test

Les tests automatiques (`python -m pytest`) utilisent de faux objets Discord : ils
prouvent la logique, **pas** le comportement réel de Discord (permissions, limites,
affichage mobile, délais). Ce plan se fait sur un **serveur de test**, jamais sur le
serveur de l'équipe. `/refonte` ne doit être lancée sur le vrai serveur qu'après ce plan,
et avec l'accord du responsable.

## 0. Préparer l'environnement de test (une fois)

1. **Serveur de test** : Discord > « + » > Créer un serveur. Recréez-y l'ancienne
   structure (au moins « 01 · ACCUEIL & ORGANISATION » avec `annonces`, et
   « 03 · DÉVELOPPEMENT » avec `developpement-python`), avec quelques messages écrits à
   la main dans chaque salon : ils serviront à vérifier que rien n'est supprimé.
2. **Bot de test** : portail développeur Discord > New Application « Samae Test » >
   Bot > Reset Token. N'utilisez **pas** le token du vrai bot : deux programmes avec le
   même token répondent tous les deux.
3. Invitez le bot de test (OAuth2 > URL Generator : `bot` + `applications.commands`,
   permissions : Gérer les salons, Gérer les rôles, Envoyer des messages, Intégrer des
   liens, Lire l'historique, Gérer les messages, Épingler).
4. Sur votre PC, dans une **copie séparée** du projet : `.env` avec le token de test,
   `DISCORD_GUILD_ID` du serveur de test, `DATABASE_PATH=data/test.sqlite3`. Lancez
   `python -m src.main`.
5. Créez un deuxième compte Discord (ou demandez à un coéquipier) **sans** droit
   d'administration, pour les tests « membre ».

Cochez chaque case et notez l'heure de tout résultat inattendu (le journal
`logs/bot.log` donne le détail à cette heure-là).

## 1. Démarrage et état

- [ ] Le journal affiche « N commandes slash synchronisées », puis « Connecté en tant que ».
- [ ] Arrêt (Ctrl+C) puis relance : « Commandes slash inchangées … rien à envoyer » ; les commandes restent utilisables.
- [ ] Lancer une deuxième fois `python -m src.main` dans un autre terminal : refus immédiat « une autre instance du bot tourne déjà ». Le premier bot continue.
- [ ] `/etat` : réponse visible par vous seul, en moins de 2 secondes, ligne « Réactivité ».
- [ ] `/etat` depuis le téléphone : lisible sans défilement horizontal.

## 2. Aide

- [ ] `/aide` : 6 pages, boutons Précédente/Suivante, menu de sections ; réponse éphémère.
- [ ] `/aide section:Réunions` ouvre directement la bonne page.
- [ ] Compte membre : les pages d'administration détaillées n'apparaissent pas.
- [ ] Le compte membre ne peut pas cliquer sur les boutons de **votre** aide (message « réservé »).
- [ ] Après 10 minutes, les boutons sont désactivés, sans message d'erreur.
- [ ] `/reunion aide` : aide des réunions sur plusieurs pages ; aucune fonction non développée (sondages, tier lists, rôles automatiques, validation des propositions de décision) n'est présentée comme disponible.

## 3. Configuration et messages permanents

- [ ] `/admin installer` : aperçu « rien n'a encore été modifié » ; **Annuler** : « rien n'a été modifié ».
- [ ] `/admin installer` puis **Confirmer** : messages permanents publiés et épinglés ; deux clics rapides sur Confirmer n'appliquent qu'une fois.
- [ ] `/admin installer` sans cliquer pendant 2 minutes : « Délai de confirmation expiré ».
- [ ] Compte membre : `/admin installer`, `/admin actualiser`, `/refonte` invisibles ou refusés (« Accès refusé »).
- [ ] Supprimer à la main un message permanent : il est recréé au bout de quelques secondes.
- [ ] `/admin actualiser` : « N message(s) modifié(s) » ; un second `/admin actualiser` : 0 modifié.
- [ ] `/admin sauvegarde` : « Sauvegarde créée … vérifiée » (la réponse peut d'abord afficher « réfléchit… »).

## 4. Réunions

- [ ] `/reunion creer titre:Test etapes:Bilan ; Démo ; Plan participants:@membre duree:30` : panneau publié dans le salon, lien dans la réponse éphémère.
- [ ] Le compte membre clique « Rejoindre » puis « Quitter » : le panneau se met à jour sur place, sans nouveau message.
- [ ] Le membre clique « Démarrer » : refusé avec une explication (réservé au responsable ou à un admin).
- [ ] Vous : « Démarrer », « Étape suivante » × 2, « Étape précédente » : l'étape en cours suit.
- [ ] Cliquer très vite deux fois sur « Étape suivante » : une seule avance, ou un message « panneau pas à jour » puis le panneau actualisé.
- [ ] `/reunion ajouter decision` (responsable) : la décision apparaît comme **décision**. Le membre ne peut pas enregistrer de décision.
- [ ] `/reunion ajouter action` avec responsable et échéance : visible sur le panneau et dans le compte rendu.
- [ ] Redémarrer le bot pendant la réunion : les boutons du panneau fonctionnent encore après le redémarrage.
- [ ] « Terminer » puis confirmer : compte rendu publié **une seule fois** dans `#comptes-rendus` (ou `#decisions` si le salon n'existe pas), lien dans la réponse ; objectif, étapes, participants, décisions, actions (responsable, échéance) lisibles.
- [ ] `/reunion compte-rendu` : même contenu, paginé si long.
- [ ] Retirer au bot la permission d'écrire dans `#comptes-rendus`, terminer une autre réunion : message « Attention, compte rendu : … », la réunion est bien terminée, rien n'est présenté comme publié.

## 5. Refonte (`/refonte lancer`, `/refonte retour`) — le plus important

À faire **uniquement sur un serveur de test** qui ressemble au vrai (anciennes catégories
numérotées, quelques salons personnels, quelques rôles, des messages écrits à la main).
Avant : notez les catégories, salons, rôles et quelques messages.

Accès et aperçu :

- [ ] Compte membre : `/refonte lancer` refusé.
- [ ] Admin **sans** « Gérer les salons » : refusé avec explication.
- [ ] `/refonte lancer` : analyse, menus « Profil du serveur » et « Style d'affichage » ; changer de style recalcule le plan ; rien n'a changé sur le serveur.
- [ ] « Prévisualiser » : structure cible avec emojis, rôles avec couleurs, renommages, déplacements, permissions, ordre, éléments inconnus conservés, risques ; aucune suppression parmi les actions.
- [ ] Un autre compte admin clique sur les boutons : refusé.
- [ ] « Annuler » : rien n'a changé. Ne rien cliquer pendant 10 minutes : vue expirée, rien n'a changé.
- [ ] Retirer au bot « Gérer les salons » : « Appliquer les changements » désactivé (point bloquant affiché). Rendre la permission.
- [ ] Retirer au bot « Gérer les rôles » : rôles et permissions passent en « à faire à la main ». Rendre la permission.
- [ ] Placer un rôle de la cible (ex. Testeur) au-dessus du rôle de Samae : signalé, non modifié.
- [ ] Prévisualiser > Appliquer, puis créer ou renommer un salon à la main **avant** de confirmer > Confirmer : « Le serveur a changé depuis l'aperçu », différences, rien appliqué.

Application :

- [ ] Appliquer > **Confirmer l'application** (double-clic rapide) : une seule exécution ; rapport avec réussites, échecs éventuels avec raison, étapes manuelles, vérification finale.
- [ ] Catégories et salons renommés avec emojis, dans l'ordre de `config/server.toml` ; historique des salons intact ; salons personnels toujours là.
- [ ] Rôles créés avec leurs couleurs, dans le bon ordre sous le rôle de Samae ; aucun rôle n'a Administrateur.
- [ ] Compte membre : ne peut pas écrire dans `#📢・annonces`, `#👋・bienvenue`, `#✅・décisions` ; ne voit pas « 🛡️ ADMINISTRATION ».
- [ ] Une permission posée à la main pour un autre rôle sur un salon : toujours là.
- [ ] Samae peut toujours écrire dans `#🧾・bot-logs` et les salons en lecture seule (`/admin diagnostic`).
- [ ] `data/backups/` contient une sauvegarde `…avantrefonte…` et `data/refonte/` un journal JSON ; `/refonte journal` l'affiche.
- [ ] `/admin actualiser` : les messages permanents citent les salons avec leurs nouveaux noms ; panneau d'une réunion ouverte avant la refonte toujours fonctionnel ; notifications GitLab/GitHub toujours reçues.
- [ ] Deuxième `/refonte lancer` : aucune action proposée (hors associations).
- [ ] Serveur Communauté : `#📢・annonces` devient un vrai salon d'annonces ; avec `facultatifs:True`, forum et conférence créés.

Suppressions :

- [ ] `/refonte lancer suppressions:True` : la catégorie « 04 · GITLAB AUTOMATISÉ » vidée est proposée **à part** ; un salon personnel avec messages n'est **pas** proposé.
- [ ] « Examiner les suppressions » puis « Annuler » : rien n'est supprimé.
- [ ] Recommencer jusqu'à « Confirmer les suppressions » : seuls les éléments listés disparaissent.

Retour arrière :

- [ ] Renommer un salon à la main, écrire dans un salon créé par la refonte, puis `/refonte retour` : l'aperçu les signale comme laissés tels quels et affiche les limites.
- [ ] Confirmer : noms, catégories, permissions, couleurs et ordre d'avant rétablis (au mieux pour l'ordre).
- [ ] « Examiner les suppressions » du retour : seuls les éléments créés **vides** sont proposés ; confirmer ; le serveur ressemble à celui d'avant.
- [ ] Sur téléphone : aperçu et rapport lisibles.

## 6. Après le test

Notez les cases non cochées et les heures des anomalies. Le test d'un serveur de test ne
garantit pas le résultat sur le vrai serveur (salons, rôles et permissions différents) :
sur le vrai serveur, lisez attentivement l'aperçu de `/refonte lancer` avant de confirmer.


## 1.4.0 : commandes regroupées, choix des rôles, accueil

À faire sur le serveur de test avant le vrai serveur. Guide : [accueil-et-roles.md](accueil-et-roles.md).

- [ ] Après redémarrage, taper `/` : seules `/aide`, `/etat`, `/roles`, `/activite`, `/ia`, `/reunion`, `/admin`, `/refonte` apparaissent (Ctrl+R si d'anciens noms restent).
- [ ] `/reunion animer etape:Démarrer` puis `etape:Étape suivante` : même effet que les boutons du panneau.
- [ ] `/reunion ajouter decision texte:Test` (responsable) ; un membre non responsable est refusé.
- [ ] `/admin actualiser` : le règlement est **modifié** dans `#annonces` (pas de second message) ; pied de page « Message géré par Samae ».
- [ ] Créer `#règlement` (`/refonte lancer facultatifs:True`), puis `/admin installer` : le règlement apparaît dans `#règlement` et disparaît de `#annonces`.
- [ ] Le panneau « Choisir mes rôles » est publié (dans `#rôles` ou `#bienvenue`) ; relancer `/admin actualiser` ne crée aucun doublon.
- [ ] Compte membre : bouton **Choisir mes rôles** → menu privé coché avec ses rôles ; cocher Développement et Art & Design : les deux rôles sont ajoutés ; décocher l'un : il est retiré.
- [ ] Le menu ne propose jamais Responsable projet ni Modérateur ; le rôle de staff d'un membre n'est jamais retiré.
- [ ] Donner « Mentionner @everyone » au rôle Testeur : il disparaît du menu et `/admin diagnostic` explique pourquoi. Retirer la permission.
- [ ] Placer le rôle de Samae sous Développement : `/admin diagnostic` le signale ; remettre l'ordre.
- [ ] Redémarrer le bot : le bouton **Choisir mes rôles** d'un ancien message fonctionne toujours.
- [ ] (Si `WELCOME_ON_JOIN=true` et l'intent activé) un compte de test rejoint : message « Bienvenue @… » dans `#bienvenue`, avec le bouton ; il quitte : le message disparaît. Avec `WELCOME_DELETE_AFTER_HOURS=1`, le message disparaît après une heure (vérification toutes les 10 minutes) et aucun autre message du salon n'est touché.
