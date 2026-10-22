"""Intégration GitLab (V2) : réception sécurisée des webhooks et notifications Discord.

Ce paquet n'importe jamais discord.py : l'envoi sur Discord passe par une
interface (``NotificationSender``) implémentée dans ``src/discord_bot/``.
"""
