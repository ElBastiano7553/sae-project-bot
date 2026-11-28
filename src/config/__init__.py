"""Chargement et validation de la configuration.

Deux sources de configuration distinctes :

* les **variables d'environnement** (fichier ``.env``) pour les secrets et
  les réglages propres à chaque machine : ``src/config/settings.py`` ;
* les **fichiers TOML** du dossier ``config/`` pour la structure du serveur,
  les liens et le planning, qui sont versionnés et partagés par l'équipe :
  ``src/config/files.py``.

Chemins du projet et ``ConfigError`` : ``src/config/base.py``. Tout reste importable
depuis ``src.config`` comme avant la 1.15.0 (où tout était dans un seul fichier).

Les fonctions de ce paquet ne dépendent pas de Discord : elles sont testables
sans token ni connexion réseau.
"""

from src.config.base import *  # noqa: F403
from src.config.files import *  # noqa: F403
from src.config.settings import *  # noqa: F403
