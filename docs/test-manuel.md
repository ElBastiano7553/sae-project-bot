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
- [ ] `/status` : réponse visible par vous seul, en moins de 2 secondes, ligne « Réactivité ».
- [ ] `/status` depuis le téléphone : lisible sans défilement horizontal.

## 2. Aide

- [ ] `/help` : 6 pages, boutons Précédente/Suivante, menu de sections ; réponse éphémère.
- [ ] `/help section:Réunions` ouvre directement la bonne page.
- [ ] Compte membre : les pages d'administration détaillées n'apparaissent pas.
- [ ] Le compte membre ne peut pas cliquer sur les boutons de **votre** aide (message « réservé »).
- [ ] Après 10 minutes, les boutons sont désactivés, sans message d'erreur.
- [ ] `/reunion help` : aide des réunions sur plusieurs pages ; aucune fonction non développée (sondages, tier lists, rôles automatiques, validation des propositions de décision) n'est présentée comme disponible.

## 3. Configuration et messages permanents

- [ ] `/setup` : aperçu « rien n'a encore été modifié » ; **Annuler** : « rien n'a été modifié ».
- [ ] `/setup` puis **Confirmer** : messages permanents publiés et épinglés ; deux clics rapides sur Confirmer n'appliquent qu'une fois.
- [ ] `/setup` sans cliquer pendant 2 minutes : « Délai de confirmation expiré ».
- [ ] Compte membre : `/setup`, `/refresh`, `/refonte` invisibles ou refusés (« Accès refusé »).
- [ ] Supprimer à la main un message permanent : il est recréé au bout de quelques secondes.
- [ ] `/refresh` : « N message(s) modifié(s) » ; un second `/refresh` : 0 modifié.
- [ ] `/admin backup` : « Sauvegarde créée … vérifiée » (la réponse peut d'abord afficher « réfléchit… »).

## 4. Réunions

- [ ] `/reunion creer titre:Test etapes:Bilan ; Démo ; Plan participants:@membre duree:30` : panneau publié dans le salon, lien dans la réponse éphémère.
- [ ] Le compte membre clique « Rejoindre » puis « Quitter » : le panneau se met à jour sur place, sans nouveau message.
- [ ] Le membre clique « Démarrer » : refusé avec une explication (réservé au responsable ou à un admin).
- [ ] Vous : « Démarrer », « Étape suivante » × 2, « Étape précédente » : l'étape en cours suit.
- [ ] Cliquer très vite deux fois sur « Étape suivante » : une seule avance, ou un message « panneau pas à jour » puis le panneau actualisé.
- [ ] `/reunion decision` (responsable) : la décision apparaît comme **décision**. Le membre ne peut pas enregistrer de décision.
- [ ] `/reunion action` avec responsable et échéance : visible sur le panneau et dans le compte rendu.
- [ ] Redémarrer le bot pendant la réunion : les boutons du panneau fonctionnent encore après le redémarrage.
- [ ] « Terminer » puis confirmer : compte rendu publié **une seule fois** dans `#comptes-rendus` (ou `#decisions` si le salon n'existe pas), lien dans la réponse ; objectif, étapes, participants, décisions, actions (responsable, échéance) lisibles.
- [ ] `/reunion compte-rendu` : même contenu, paginé si long.
- [ ] Retirer au bot la permission d'écrire dans `#comptes-rendus`, terminer une autre réunion : message « Attention, compte rendu : … », la réunion est bien terminée, rien n'est présenté comme publié.

## 5. Refonte (`/refonte`) — le plus important

Avant : notez le nombre de catégories, salons, rôles, et quelques messages écrits à la main.

- [ ] Compte membre : `/refonte` refusé.
- [ ] Admin **sans** « Gérer les salons » : refusé avec explication.
- [ ] `/refonte` : analyse « rien n'a été modifié » ; « Prévisualiser » : plan paginé, dernière ligne « 0 suppression ».
- [ ] « Annuler » : rien n'a changé sur le serveur.
- [ ] Ouvrir `/refonte`, ne rien cliquer pendant 10 minutes : vue expirée, rien n'a changé.
- [ ] Retirer au bot « Gérer les salons » : « Appliquer » est désactivé (point bloquant affiché).
- [ ] Rendre la permission ; `/refonte` > Prévisualiser > Appliquer, puis créer un salon à la main **avant** de confirmer > Confirmer : « le serveur a changé », nouveau plan, rien appliqué.
- [ ] `/refonte` > Prévisualiser > Appliquer > **Confirmer l'application** (double-clic rapide) : une seule exécution ; rapport avec réussites, échecs éventuels avec raison, étapes manuelles.
- [ ] Après : **aucun** salon, rôle, catégorie ou message supprimé ; les messages écrits à la main sont toujours là, dans les salons renommés ; l'historique est intact.
- [ ] Catégorie ADMINISTRATION invisible pour le compte membre.
- [ ] `data/backups/` contient une sauvegarde `…avantrefonte…` et `data/refonte/` un journal JSON.
- [ ] `/refresh` : les messages permanents citent les salons avec leurs nouveaux noms.
- [ ] Panneau d'une réunion ouverte avant la refonte : boutons toujours fonctionnels.
- [ ] Deuxième `/refonte` : le plan ne propose plus que des éléments « déjà conformes » (aucune création ni aucun renommage).
- [ ] `/refonte roles:True` : rôles proposés créés **sans aucune permission** (Paramètres du serveur > Rôles).
- [ ] Sur téléphone : aperçu et rapport lisibles.

## 6. Après le test

Notez les cases non cochées et les heures des anomalies. Le test d'un serveur de test ne
garantit pas le résultat sur le vrai serveur (salons, rôles et permissions différents) :
sur le vrai serveur, lisez attentivement l'aperçu de `/refonte` avant de confirmer.
