"""Intégration GitHub : notifications du dépôt du jeu, reçues par webhooks signés.

Indépendante de l'intégration GitLab (``src/gitlab/``) : son propre secret, son
propre port, sa propre table d'historique. Elle réutilise seulement les outils
neutres de GitLab (nettoyage des textes, objet ``Notification``, envoi Discord).

Ce paquet n'importe jamais discord.py : l'envoi sur Discord passe par une
interface (``NotificationSender``) implémentée dans ``src/discord_bot/``.
"""
