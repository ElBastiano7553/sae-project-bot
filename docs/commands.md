# Commandes du bot et permissions

Toutes les réponses sont **éphémères** : seule la personne qui lance la commande
les voit (exceptions : le panneau et le compte rendu des réunions, publics). Aucune commande ne peut notifier `@everyone`, un rôle ou un membre :
les mentions sont désactivées pour tous les messages du bot.

« Admins du bot » : membres ayant la permission Discord **Gérer le serveur**, ou
le rôle indiqué dans `ADMIN_ROLE_NAME`. Les commandes d'administration sont
protégées deux fois :

1. Discord les masque par défaut aux membres sans « Gérer le serveur »
   (un administrateur du serveur peut changer cette visibilité dans
   *Paramètres du serveur > Intégrations*) ;
2. le bot **revérifie** les droits à chaque appel, quelle que soit la visibilité.

## Consultation (tous les membres)

| Commande | Effet |
|---|---|
| `/help` | Aide par catégorie, avec exemples. Les commandes d'administration n'y figurent que pour les admins du bot. Un test vérifie que l'aide correspond exactement aux commandes enregistrées. |
| `/status` | État mesuré à l'instant : connexion Discord (latence), base SQLite (version du schéma), serveur Discord et salons, messages permanents (publiés, en erreur, liens à compléter), webhooks GitLab (écoute locale ; l'accès depuis Internet n'est **pas** vérifié), IA (fournisseur, modèle, demandes du jour ; la clé n'est pas testée pour ne pas consommer de demande), historique, alertes, dernière erreur (une ligne, sans trace d'appel ni secret), version et durée de fonctionnement. Répondre à `/status` ne prouve pas que le bot est disponible en continu. |
| `/health` | Ancien nom de `/status`, conservé pour compatibilité. |
| `/project` | Liens configurés (et nombre de liens à compléter), branche principale d'après le dernier webhook reçu, dernier pipeline sur cette branche, dernières merge requests, derniers pipelines et derniers pushs **reçus par le bot**. Aucun statut global n'est calculé. |

## Activité GitLab (tous les membres)

Calculs détaillés et limites : [history-and-stats.md](history-and-stats.md).

| Commande | Effet |
|---|---|
| `/events [type] [periode] [recherche]` | 15 derniers événements GitLab reçus, filtrés par type (pushs, MR, pipelines, déploiements), période (24 h à tout l'historique, 7 jours par défaut) et texte (titre, branche, auteur). Le type « Erreurs du bot » est réservé aux admins du bot. |
| `/stats [periode]` | Commits, pushs, MR ouvertes et fusionnées, pipelines terminés, déploiements, écart avec la période précédente quand l'historique la couvre. |
| `/leaderboard [periode] [details]` | Contributions par membre : MR fusionnées, approbations, jours actifs. Les commits ne comptent pas. Pas de classement sans assez de données. |

## Assistant IA (tous les membres, si `AI_PROVIDER` est configuré)

Configuration, coûts et sécurité : [ai.md](ai.md).

| Commande | Effet |
|---|---|
| `/ask question` | Réponse de l'IA à partir des documents de l'équipe (`knowledge/`, planning, liens, messages permanents, docs), avec les sources citées. |
| `/summary [periode]` | Bilan de l'activité GitLab (24 h, 7 jours, 30 jours) rédigé à partir des événements reçus. |
| `/review mr` | Relecture d'une merge request lue sur GitLab en lecture seule (nécessite `GITLAB_ACCESS_TOKEN`). |

Quotas : `AI_DAILY_LIMIT` demandes par jour pour l'équipe, `AI_COOLDOWN_SECONDS` entre
deux demandes d'un membre, une demande en cours par membre.

## Réunions (Samae 2.0, lot 1)

Guide complet : [meetings.md](meetings.md). L'option `reunion` est facultative : par
défaut, la réunion prévue ou en cours de ce salon (ou celle dont vous êtes participant).

| Commande | Qui | Effet |
|---|---|---|
| `/reunion creer titre [objectifs] [etapes] [responsable] [participants] [duree] [description]` | tous | Crée la réunion et publie son panneau dans le salon courant. Étapes séparées par « ; », participants mentionnés avec `@`. |
| `/reunion voir [reunion] [publier]` | tous ; `publier` : responsable ou admin | Affiche la réunion ; `publier:True` déplace le panneau dans ce salon (nouveau panneau publié avant la suppression de l'ancien). |
| `/reunion liste [statut]` | tous | Réunions prévues ou en cours, terminées, annulées ou toutes. |
| `/reunion rejoindre` · `quitter` | tous | Rejoindre ou quitter (aussi par les boutons du panneau). |
| `/reunion inviter membre` | responsable ou admin | Ajoute un participant. |
| `/reunion demarrer` · `suivante` · `precedente` | responsable ou admin | Déroulement de l'ordre du jour (aussi par les boutons). |
| `/reunion progression` | tous | Étapes traitées, étape en cours, temps écoulé et durée prévue. |
| `/reunion point texte` | participants | Ajoute un point à la fin de l'ordre du jour. |
| `/reunion decision texte [motif]` | responsable ou admin | Enregistre une décision (réunion en cours). |
| `/reunion reserve texte [type]` | participants | Enregistre une réserve ou un désaccord (réunion en cours). |
| `/reunion action description responsable [echeance]` | participants | Ajoute une action à faire et son responsable (réunion en cours). |
| `/reunion terminer` · `annuler` | responsable ou admin | Clôture après confirmation (2 min) ; `terminer` fige et publie le compte rendu. |
| `/reunion compte-rendu [reunion] [publier]` | tous ; `publier` : responsable ou admin | Compte rendu paginé (aperçu provisoire avant la fin) ; `publier:True` republie un compte rendu supprimé. |

## Administration (admins du bot)

| Commande | Effet | Confirmation |
|---|---|---|
| `/setup` | Compare `config/server.toml` au serveur, puis enregistre les salons reconnus et publie ou met à jour les messages permanents. Ne crée ni ne supprime aucun salon. | Prévisualisation + bouton **Confirmer** (2 min) |
| `/refresh [message]` | Met à jour les messages permanents **sur place** (même lien, réactions conservées) et recrée ceux qui ont été supprimés. `message` limite l'actualisation à un seul message. | Non (aucune perte possible) |
| `/config` | Réglages (sans aucun secret), salons associés, liens vers les messages gérés. | — |
| `/announce` | Formulaire pour rédiger l'annonce en cours. Le message « Annonce en cours » de `#annonces` est **modifié**, jamais republié. | Prévisualisation + **Confirmer** |
| `/admin messages` | État de chaque message permanent : lien, dernière action, dernière vérification, dernière erreur, liens à compléter. | — |
| `/admin rebuild message` | Publie un nouveau message puis supprime l'ancien (le lien et les réactions sont perdus). À réserver aux messages qui ne peuvent plus être modifiés. | Avertissement + **Confirmer** |
| `/admin diagnostic` | Permissions du bot dans chaque salon utilisé (messages permanents, notifications GitLab) et état des intégrations. | — |
| `/admin history` | 15 dernières actions d'administration (qui, quoi, quand, résultat). | — |
| `/admin backup` | Sauvegarde vérifiée de la base, tout de suite (nom, taille, nombre de sauvegardes conservées). Rien n'est supprimé hors des anciennes sauvegardes du bot. | — |

## Permissions Discord nécessaires au bot

Le bot n'a **pas** besoin de la permission Administrateur ni d'aucun intent privilégié.

| Salons | Permissions |
|---|---|
| Salons des messages permanents (`#bienvenue`, `#annonces`, `#planning-et-deadlines`, `#decisions-et-comptes-rendus`, `#documentation-et-preuves`) | Voir le salon, Envoyer des messages, Intégrer des liens, Voir les anciens messages, Épingler des messages (si `PIN_PERMANENT_MESSAGES=true`) |
| Salons des notifications GitLab (`#gitlab-activite`, `#builds-et-tests`) | Voir le salon, Envoyer des messages, Intégrer des liens (et Voir les anciens messages, Épingler pour le tableau de bord de `#gitlab-activite`) |
| Salon des alertes (`ALERTS_CHANNEL`, facultatif) | Voir le salon, Envoyer des messages, Intégrer des liens |
| Salons où sont créées les réunions, et `#decisions-et-comptes-rendus` (comptes rendus) | Voir le salon, Envoyer des messages, Intégrer des liens, Voir les anciens messages (pour modifier le panneau) |

`/admin diagnostic` indique, salon par salon, les permissions qui manquent.
