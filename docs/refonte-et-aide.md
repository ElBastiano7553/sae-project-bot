# Refonte du serveur, centre d'aide et identité visuelle (versions 1.2.0 et 1.3.0)

Ce document décrit `/refonte`, `/aide`, `/reunion aide` et la nouvelle apparence de
Samae : ce qui a été audité, comment l'utiliser, comment revenir en arrière et ce qui
reste à faire à la main. Rien n'a été testé sur le vrai serveur Discord : les tests
utilisent un faux serveur en mémoire.

## 1 à 6. Refonte du serveur

Depuis la version 1.3.0, `/refonte` a été entièrement revue (emojis et styles, rôles
colorés, permissions, ordre, doublons, suppressions séparées, retour arrière) et devient
un groupe de commandes : `/refonte lancer`, `/refonte retour`, `/refonte journal`.
Tout ce qui la concerne (audit, architecture, configuration, permissions, sauvegarde,
retour arrière, tests et limites) est dans **[refonte.md](refonte.md)**.

## 7. Centre d'aide

- `/aide [section]` : 6 pages (Accueil, Réunions, Commandes, Organisation,
  Administration, Dépannage), un menu de sections, Précédent / Accueil / Suivant et
  l'indicateur de page. Les sections d'administration n'apparaissent que pour les
  admins du bot.
- `/reunion aide [sujet]` : 6 pages (Créer, Participer, Animer, Décisions et réserves,
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

- `tests/test_help_center.py` : contenu, limites Discord, navigation de `/aide`.
- Tests de `/refonte` : voir [refonte.md](refonte.md#10-tests).
- Suite complète (1.3.0) : 736 tests verts.

## 10. Lancer et synchroniser les commandes (Windows)

```powershell
.\.venv\Scripts\Activate.ps1
python -m pytest -q
python -m src.main
```

Au démarrage, le bot synchronise ses commandes sur le serveur `DISCORD_GUILD_ID` (depuis 1.2.1, seulement si elles ont changé) :
`/refonte lancer`, `/aide section:` et `/reunion aide` apparaissent tout de suite. Si une
commande n'apparaît pas, redémarrez Discord (Ctrl+R).
