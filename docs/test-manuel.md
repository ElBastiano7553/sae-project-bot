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

## 1.5.0 : projets suivis (`/projet`, multi-projets étape B1)

Guide : [projets.md](projets.md). Faire d'abord une copie de la base, ou vérifier que la sauvegarde `…-avantv9.sqlite3` est créée au démarrage.

- [ ] Au démarrage, le journal indique « Migration de la base de données vers la version 9 » ; `/etat` affiche « schéma v9 ».
- [ ] `/projet liste` sans webhook configuré : « Aucun projet n'est suivi sur ce serveur ».
- [ ] Avec `GITHUB_WEBHOOK_SECRET` (et/ou `GITLAB_WEBHOOK_SECRET`) dans `.env` : `/projet liste` montre le projet historique (`env-github`, `env-gitlab`) ; aucun secret n'apparaît.
- [ ] `/projet details projet:env-github` en administrateur : salons des notifications et ligne « Configuration » (.env, port).
- [ ] Même commande avec un compte membre qui ne voit pas l'un des salons : ce salon n'est pas nommé, « salon(s) masqué(s) » est indiqué ; pas de ligne « Configuration ».
- [ ] `/projet details projet:999` : « Projet introuvable sur ce serveur ».
- [ ] Les notifications GitHub et GitLab existantes arrivent comme avant (rien n'a changé dans leur configuration).

## 1.6.0 : salons et filtres par projet (`/projet modifier`, étape B2)

Ajouter d'abord un projet avec `/projet ajouter` (section 1.7.0 ci-dessous).

- [ ] Compte membre : `/projet modifier` est refusé (« réservée aux administrateurs ») et n'apparaît pas dans `/aide` > Commandes.
- [ ] `/projet modifier projet:env-github …` : « Projet historique », rien n'est changé ; les notifications GitHub arrivent comme avant.
- [ ] `/projet modifier projet:<n> categorie:Code (commits) salon:#activité-git` puis `branches:main, develop` : la réponse montre « Avant » / « Maintenant » ; `/projet details` affiche le filtre.
- [ ] Un salon où Samae ne peut pas écrire est refusé avec la permission manquante ; un salon vocal est refusé.
- [ ] `filtre:explose` : message listant les valeurs possibles, rien n'est modifié.
- [ ] `retirer:True` : la catégorie disparaît de `/projet details` ; `/admin historique` montre chaque modification.

## 1.7.0 : ajouter un projet (`/projet ajouter`)

- [ ] Compte membre : `/projet ajouter` est refusé.
- [ ] `/projet ajouter url:https://github.com/<equipe>/<depot>` : récapitulatif privé (GitHub, nom, nom du serveur, « Surveillance : pas encore ») ; rien dans `/projet liste` avant confirmation.
- [ ] « Annuler » ou attendre 2 minutes : rien n'est enregistré ; un autre compte qui clique est refusé.
- [ ] « Confirmer » : le projet apparaît dans `/projet liste` avec « pas encore connecté : aucune notification » ; `/admin historique` montre l'ajout.
- [ ] Relancer avec la même adresse en majuscules : « Projet déjà suivi ».
- [ ] Adresse du dépôt du `.env` (`GITHUB_WEBHOOK_REPOSITORY`) : refusée (projet historique).
- [ ] `url:http://…`, `url:https://bitbucket.org/a/b`, `url:https://nom:motdepasse@github.com/a/b` : message clair, l'adresse fautive n'est pas répétée.
- [ ] Adresse GitLab (`https://gitlab.com/groupe/projet`) : fournisseur GitLab détecté.
- [ ] Puis `/projet modifier` sur ce projet (section 1.6.0).

## 1.7.1 : projets masqués aux membres

Préparer deux projets : « Public » avec un salon que tout le monde voit, « Privé » avec un salon qu'un compte membre de test ne voit pas, plus un projet sans salon.

- [ ] Compte membre : `/projet liste` ne montre que « Public » (« 1 projet sur ce serveur »).
- [ ] Compte membre : `/projet details projet:<référence de Privé>` répond « Projet introuvable », comme `/projet details projet:999`.
- [ ] Compte membre : en tapant `/projet details projet:`, l'autocomplétion ne propose que « Public » ; `/projet modifier projet:` ne propose rien.
- [ ] Compte administrateur : les trois projets apparaissent dans la liste, les détails et l'autocomplétion.
- [ ] Donner au membre l'accès au salon de « Privé » : le projet apparaît pour lui sans redémarrage.

## 1.8.0 : réception commune des événements (étape B3)

Guide : [projets-reception.md](projets-reception.md). Faire d'abord une copie de la base, ou vérifier que la sauvegarde `…-avantv10.sqlite3` est créée au démarrage. Aucune manipulation Nginx n'est nécessaire.

- [ ] Sans rien changer au `.env` : au démarrage, le journal indique « Migration de la base de données vers la version 10 » puis « Réception des projets désactivée » ; `/etat` affiche « schéma v10 » et « Réception des projets : désactivée ».
- [ ] Les notifications GitHub (8081) et GitLab (8080) du `.env` arrivent comme avant.
- [ ] `PROJECTS_HTTP_ENABLED=true` puis redémarrage : `/etat` affiche « écoute locale sur http://127.0.0.1:8090, GitHub App fermée ».
- [ ] Sur le serveur qui héberge Samae : `curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8090/gitlab/webhook/essai-0000000000000` affiche `401` ; `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8090/` affiche `404`.
- [ ] Depuis un autre ordinateur, `http://<adresse du VPS>:8090/` ne répond pas (le port n'écoute qu'en local).
- [ ] `PROJECTS_HTTP_HOST=0.0.0.0` : `/etat` affiche une erreur « adresse locale », le reste du bot fonctionne.
- [ ] `/projet liste` : les projets ajoutés restent « enregistré, pas encore connecté : aucune notification » (la connexion arrive en B4/B5).

## 1.9.0 : connexion des projets GitHub (`/projet configurer`, étape B4)

Guide : [github-app.md](github-app.md). Sauvegarde `…-avantv11.sqlite3` créée au démarrage.

Sans configuration (rien à faire sur le VPS) :

- [ ] Au démarrage : « Migration de la base de données vers la version 11 » ; `/etat` affiche « schéma v11 ».
- [ ] `/projet configurer` (admin) : « connexion GitHub n'est pas encore activée », rien n'est créé.
- [ ] Compte membre : `/projet configurer` n'apparaît pas, ou est refusée ; l'option `depot` ne propose rien.
- [ ] Les notifications des webhooks du `.env` (8080, 8081) arrivent comme avant.

Avec la GitHub App de test (après accord du propriétaire du VPS : DNS, certificat, site Nginx) :

- [ ] `/etat` : « connexion GitHub active ».
- [ ] `/projet configurer` : réponse visible par vous seul, boutons « Installer sur GitHub » et « Choisir le dépôt », aucun secret affiché.
- [ ] Installation sur le **seul dépôt de test**, autorisation demandée par GitHub, page « Installation vérifiée ».
- [ ] Le même lien ouvert une seconde fois : page « Lien expiré » (usage unique).
- [ ] « Choisir le dépôt » : seul le dépôt de test est proposé ; confirmation ; état « connecté : notifications actives ».
- [ ] `/projet modifier` : salon pour Code (`branches:main`), Revues et Tickets ; « Envoyer un message de test » (si le bouton a expiré, `/projet configurer depot:<dépôt>` le propose à nouveau) ou `python -m src.providers.check … --filtre` : tout ✅, un seul message.
- [ ] Push sur `main` du dépôt de test : notification dans le salon Code. Push sur une autre branche : rien.
- [ ] Pull request ouverte, ticket ouvert : notifications dans leurs salons.
- [ ] GitHub > App > Advanced > Recent Deliveries > Redeliver d'une livraison déjà reçue : réponse `duplicate`, aucun second message.
- [ ] Un compte GitHub sans accès au dépôt de test suit un lien de `/projet configurer` : page « Accès refusé », rien de relié.
- [ ] Un compte GitHub avec seulement le rôle Read ou Write sur le dépôt de test : page « Droits insuffisants », rien de relié.
- [ ] Ouvrir le même lien d'installation dans deux onglets en même temps : un seul aboutit, l'autre affiche « Lien expiré ».
- [ ] Journal `logs/bot.log` et journal Nginx du site : aucun `code=`, `state=`, token ni secret.

### `/projet diagnostic` (audit d'avant B5)

- [ ] Compte membre : `/projet diagnostic` n'apparaît pas, ou est refusée.
- [ ] Projet connecté qui n'a encore rien reçu : quatre lignes, la dernière en ⚠️, piste « Recent deliveries ».
- [ ] Retirer à Samae « Envoyer des messages » dans le salon Code : ligne « Salons » en ❌ qui nomme la permission.
- [ ] Après un push : « 1 publiée(s) ». Avec un filtre de branche qui l'écarte : « écartée(s) par les filtres ».
- [ ] `/projet diagnostic projet:env-github` : « introuvable », renvoi vers `/admin diagnostic`. Aucun secret affiché nulle part.

## 1.10.0 : connexion des projets GitLab (`/projet configurer projet:`, étape B5)

Guide : [gitlab-app.md](gitlab-app.md). Sauvegarde `…-avantv12.sqlite3` créée au démarrage.

Sans configuration :

- [ ] `/etat` : « connexion GitLab non configurée » ; webhook GitLab historique inchangé (un push du `.env` arrive comme avant).
- [ ] `/projet configurer projet:<projet GitLab>` (admin) : « connexion GitLab n'est pas encore activée », rien n'est créé.

Avec l'application OAuth de test (section 3 du guide) et l'adresse HTTPS :

- [ ] `/etat` : « connexion GitLab active ». Une variable fausse : « connexion GitLab refusée (raison) », le reste démarre.
- [ ] Compte membre : l'option `projet` ne propose rien, la commande est refusée.
- [ ] `/projet ajouter url:<projet GitLab de test>`, `/projet modifier` (salon Code), puis `/projet configurer projet:<réf>` : réponse visible par vous seul, bouton « Se connecter avec GitLab ».
- [ ] Autoriser avec un compte Maintainer : page « Projet relié » avec l'URL et le jeton secret. Recharger : « Lien expiré », le secret n'apparaît plus.
- [ ] Créer le webhook dans GitLab (Push, MR, Pipeline, Deployment, SSL activé) → « Tester » → « Événements de poussée » : une notification dans le salon Code. « Tester » une seconde fois avec le même événement : une notification de plus seulement si GitLab envoie un nouvel identifiant de livraison.
- [ ] Compte Developer du projet : page « Droits insuffisants », rien de relié. Compte sans accès : « Accès refusé ».
- [ ] Annuler sur GitLab : « Autorisation annulée ». Lien de plus de 15 minutes : « Lien expiré ».
- [ ] Relancer la connexion d'un projet relié : avertissement « nouveau secret » dans Discord ; l'ancien secret donne 401 dans « Événements récents », le nouveau fonctionne.
- [ ] `logs/bot.log`, journal Nginx, `sqlite3 data/sae_bot.sqlite3 .dump` : ni le secret du webhook, ni token, ni `code=`/`state=`.
- [ ] Projet du `.env` (`GITLAB_PROJECT_ID`) : refusé avec « env-gitlab ».


## 1.11.0 : musique (`/musique`)

Retirée en 1.13.0 : plus rien à tester.


## 1.12.0 : multi-serveurs, `/setup`

À faire sur un **serveur Discord de test** avec un **bot de test**. Guide :
[installation.md](installation.md). (Les tests de musique de 1.12.0 n'ont plus lieu d'être : musique retirée en 1.13.0.) Sauvegarde `…-avantv14.sqlite3` créée au démarrage.

- [ ] Journal au démarrage : liste des fonctions facultatives et leur état ; aucun secret.
- [ ] `/setup etat` sur le serveur principal et sur un second serveur de test ; aucun secret affiché.
- [ ] Second serveur : `/reunion` et `/projet liste` ne montrent que les données de ce serveur ; `/admin`, `/refonte`, `/ia`, `/activite`, `/roles` absents.
- [ ] `/setup serveur-principal` par un admin qui n'est pas le propriétaire du bot : refus.
- [ ] Installation de zéro sur une autre machine en suivant [installation.md](installation.md), avec seulement `DISCORD_BOT_TOKEN` dans `.env`.


## 1.13.0 : musique retirée

À faire sur un **serveur Discord de test** avec un **bot de test**, avec un `.env` qui contient encore les anciennes
variables `MUSIC_*` / `LAVALINK_*` (elles doivent être ignorées).

- [ ] `pip install -r requirements.txt` dans un environnement neuf : ni `wavelink` ni `async-timeout` installés.
- [ ] Le bot démarre sans Lavalink ; journal : aucune ligne « Musique » ni « Lavalink » ; aucun secret.
- [ ] Commandes : `/play`, `/search`, `/join`, `/audiocheck`, `/skip`, `/queue`… ont disparu (redémarrer Discord si besoin) ; `/setup` ne propose plus que `etat` et `serveur-principal`.
- [ ] `/aide` : plus de catégorie « Musique » ; `/etat` et `/setup etat` : plus de ligne « Musique ».
- [ ] `/reunion`, `/projet liste`, `/admin config`, `/etat` fonctionnent comme avant.
