"""Réception commune des événements des projets enregistrés (multi-projets, étape B3).

* ``translate`` : traduit un événement GitHub ou GitLab en ``ProjectEvent`` (catégorie
  commune, action ou statut dans le vocabulaire commun, branche, notification) ;
* ``dispatcher`` : filtres de chaque projet, anti-doublons par projet, file d'envoi ;
* ``server`` : serveur HTTP sur 127.0.0.1 (``PROJECTS_HTTP_PORT``, 8090 par défaut).

Les webhooks historiques du ``.env`` (``src/github``, ``src/gitlab``, ports 8081 et 8080)
ne passent pas par ici et ne changent pas.
"""
