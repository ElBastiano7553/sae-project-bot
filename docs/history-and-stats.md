# Historique, statistiques, classement, tableau de bord et alertes (lot 2)

Toutes ces fonctions utilisent **uniquement les webhooks GitLab reçus par le bot**.
Le bot ne connaît rien de ce qui s'est passé avant l'activation des webhooks, ni
rien de ce que GitLab ne lui a pas envoyé (bot arrêté, webhook désactivé…). Chaque
réponse indique sa période et le nombre d'événements sur lesquels elle repose.

## 1. Ce qui est enregistré

| Événement GitLab (case à cocher du webhook) | Enregistré | Notifié sur Discord |
|---|---|---|
| Push events | chaque push et chaque création de branche | selon `[push] branches` de `config/gitlab.toml` |
| Merge request events | toutes les actions (ouverture, mise à jour, approbation, fusion…) | selon `[merge_request] actions` |
| Pipeline events | statuts **finaux** (réussi, échoué, annulé, ignoré, manuel) | selon `[pipeline] statuses` et `branches` |
| Deployment events (**nouveau**) | réussi, échoué, annulé | selon `[deployment] statuses` (salon : celui des pipelines par défaut) |

Un événement filtré pour Discord (branche non suivie, action non notifiée) est quand
même **conservé** pour l'historique, avec le statut `recorded` : GitLab reçoit alors
la réponse `{"status": "recorded"}`. Les pipelines « en cours » ne sont pas conservés.

Pour les déploiements, cochez **Deployment events** dans
*GitLab > Settings > Webhooks*. Le bot n'annonce jamais un déploiement à partir
d'un pipeline réussi : seule cette case fournit une information fiable.

### Durée de conservation

`HISTORY_RETENTION_DAYS` (défaut **365**, minimum 30) s'applique à l'historique GitLab,
au journal d'administration et aux erreurs. Le nettoyage a lieu au démarrage puis une
fois par jour. Avant le lot 2, les événements étaient oubliés après 30 jours : la
nouvelle valeur conserve **plus** de données, rien n'est supprimé en plus.

## 2. Commandes

| Commande | Options | Réponse |
|---|---|---|
| `/events` | `type` (pushs, MR, pipelines, déploiements, erreurs), `periode` (24 h, 7 j, 30 j, 90 j, tout ; 7 j par défaut), `recherche` (titre, branche ou auteur) | 15 événements au plus, les plus récents d'abord. *Non notifié* = conservé sans notification. Le type « erreurs » est réservé aux admins du bot. |
| `/stats` | `periode` (7 j par défaut) | Commits, pushs, MR ouvertes et fusionnées, pipelines terminés et taux de réussite, déploiements. Écart avec la période précédente **uniquement si** l'historique la couvre entièrement. Détail par semaine pour 30 j et plus. |
| `/leaderboard` | `periode` (30 j par défaut), `details` | Contributions par membre (voir ci-dessous). |

## 3. Comment les chiffres sont calculés

- **Pipeline** : compté une seule fois, avec son **dernier** statut final reçu (un
  pipeline échoué puis relancé avec succès compte comme réussi).
- **Merge request** : comptée une fois par MR (numéro `!iid`), même si GitLab envoie
  plusieurs fois la même action.
- **Commits** : nombre indiqué par GitLab dans chaque push (`total_commits_count`).

### Classement (`/leaderboard`)

Ordre de tri, du plus important au moins important :

1. **MR fusionnées**, attribuées à leur **auteur** (pas à la personne qui clique sur
   « Merge ») ;
2. **approbations** données aux MR des **autres** (une par MR, retirée si l'approbation
   est annulée ; approuver sa propre MR ne compte pas) ;
3. **jours actifs** : jours distincts avec au moins un push (pousser 20 fois le même
   jour compte comme un seul jour).

Le nombre de commits est affiché avec `details:True`, à titre indicatif, mais
**n'entre pas** dans le classement : découper son travail en beaucoup de petits
commits ne doit pas faire monter.

Aucun classement n'est affiché s'il y a moins de **5 événements** sur la période
ou moins de **2 membres** actifs : la liste est alors présentée par ordre
alphabétique, avec la raison. Deux membres aux valeurs identiques ont le même rang.

Le classement est un outil de suivi pour l'équipe, **pas une évaluation** : le travail
fait hors GitLab (game design, graphismes, réunions, documentation sur Discord…)
n'y apparaît pas.

Les noms affichés sont ceux envoyés par GitLab (nom du compte). Une MR fusionnée dont
GitLab n'indique pas l'auteur est signalée « non attribuée ».

## 4. Tableau de bord permanent (`#gitlab-activite`)

Le message « 📊 Tableau de bord du projet » (fichier
`content/messages/tableau-de-bord.toml`) est publié par `/setup` comme les autres
messages permanents. Il affiche :

- la branche principale et le statut de son dernier pipeline ;
- les MR ouvertes (dernier événement reçu ni fusion ni fermeture) ;
- les branches dont le dernier pipeline a échoué ;
- les derniers déploiements par environnement ;
- l'activité des 7 derniers jours ;
- la source des données (nombre d'événements, depuis quand).

Il est **modifié sur place** 15 secondes après un nouvel événement (une rafale
d'événements ne produit qu'une modification), et vérifié toutes les 30 minutes.
Il n'est modifié que si son contenu change. `/refresh message:tableau-de-bord`
force une vérification.

## 5. Alertes d'erreurs répétées

Toute erreur du bot (niveau ERROR des journaux) est enregistrée dans la base,
regroupée par source et message (les nombres sont ignorés : « salon 12 introuvable »
et « salon 34 introuvable » sont la même erreur). Les admins la consultent avec
`/events type:Erreurs du bot`.

Si `ALERTS_CHANNEL` est défini, une alerte est publiée dans ce salon quand **une même
erreur survient 3 fois en 10 minutes**, avec au plus **une alerte par erreur et par
heure** et **5 alertes par heure** au total. Une alerte ne mentionne personne.

`ALERTS_CHANNEL` est une **clé** de `config/server.toml` (par exemple `bilan_ia` ou
`bugs`), pas un nom de salon : le salon doit avoir été reconnu par `/setup`.
`/status` indique si les alertes sont actives.

Le bot a besoin, dans ce salon, des permissions Voir le salon, Envoyer des messages
et Intégrer des liens.

## 6. Ce que le bot ne sait pas

- L'activité antérieure à l'activation des webhooks, ou survenue pendant un arrêt
  du bot si GitLab n'a pas renvoyé les événements.
- L'historique complet d'un dépôt (il n'interroge pas l'API GitLab dans ce lot).
- Le temps passé ou la qualité du travail : aucun chiffre ne doit être lu comme une note.
