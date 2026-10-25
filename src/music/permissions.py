"""Qui peut faire quoi avec le lecteur (règles appliquées à CHAQUE commande et à CHAQUE clic).

Les informations viennent de Discord au moment de l'interaction (salon vocal réel de la
personne, rôles réels, permission « Gérer le serveur ») : jamais du contenu d'un message ou
d'un bouton, qui ne sert qu'à reconnaître le lecteur et le titre affichés.

Règles :

* **Écouter, ajouter, mettre en pause, reprendre** : toute personne présente dans le salon
  vocal du bot (ou, si le bot n'est pas connecté, dans n'importe quel salon vocal du serveur).
* **Passer un titre** : la personne qui l'a demandé, ou une personne « DJ ».
* **Arrêter et vider la file, changer le volume, faire partir le bot** : une personne « DJ ».
* **Est « DJ »** : un administrateur du bot (« Gérer le serveur » ou ADMIN_ROLE_NAME), une
  personne qui a le rôle DJ du serveur (choisi par /setup role-dj, sinon MUSIC_DJ_ROLE_NAME),
  ou la personne seule avec le bot dans le salon. Sans rôle DJ (réglage par défaut, adapté à
  une petite équipe), toute personne présente dans le salon vocal du bot est « DJ ».
* **Consulter** (titre en cours, file, historique, favoris) : tout membre du serveur ; les
  réponses sont éphémères et ne concernent que ce serveur.
* Un administrateur du bot peut arrêter la musique ou faire partir le bot sans être dans le salon.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.music.errors import ControlDenied, NotInVoice, WrongVoiceChannel
from src.music.models import QueueEntry
from src.music.player import GuildPlayer


@dataclass(frozen=True)
class Listener:
    """La personne qui agit, telle que Discord la décrit au moment de l'interaction."""

    user_id: int
    name: str
    voice_channel_id: int | None  # salon vocal où elle se trouve (dans CE serveur)
    is_admin: bool = False
    has_dj_role: bool = False
    # Nombre de personnes (hors bots) dans son salon vocal, elle comprise.
    humans_in_channel: int = 0
    # Ce serveur a-t-il un rôle DJ ? None : le réglage global du service s'applique.
    dj_role_configured: bool | None = None


@dataclass(frozen=True)
class MusicPolicy:
    dj_role_configured: bool = False

    def require_voice(self, listener: Listener) -> int:
        if listener.voice_channel_id is None:
            raise NotInVoice("Rejoignez d'abord un salon vocal du serveur.")
        return listener.voice_channel_id

    def require_same_channel(self, player: GuildPlayer | None, listener: Listener) -> None:
        """La personne doit être avec le bot (si le bot est connecté)."""
        channel = self.require_voice(listener)
        if player is not None and player.connected and player.voice_channel_id != channel:
            raise WrongVoiceChannel(
                f"Le bot joue déjà dans <#{player.voice_channel_id}> : rejoignez ce salon pour le contrôler."
            )

    def is_dj(self, player: GuildPlayer | None, listener: Listener) -> bool:
        if listener.is_admin or listener.has_dj_role:
            return True
        in_bot_channel = player is not None and player.connected and listener.voice_channel_id == player.voice_channel_id
        if in_bot_channel and listener.humans_in_channel <= 1:
            return True  # seule avec le bot : personne d'autre n'est gêné
        configured = self.dj_role_configured if listener.dj_role_configured is None else listener.dj_role_configured
        return in_bot_channel and not configured

    def require_dj(self, player: GuildPlayer | None, listener: Listener, action: str, *, admin_anywhere: bool = False) -> None:
        if admin_anywhere and listener.is_admin:
            return
        self.require_same_channel(player, listener)
        if not self.is_dj(player, listener):
            raise ControlDenied(
                f"{action[:1].upper()}{action[1:]} est réservé aux personnes « DJ » (rôle configuré par les administrateurs) "
                "ou à la personne seule avec le bot dans le salon."
            )

    def require_skip(self, player: GuildPlayer | None, listener: Listener, current: QueueEntry | None) -> None:
        self.require_same_channel(player, listener)
        if current is not None and current.requester_id == listener.user_id:
            return
        if not self.is_dj(player, listener):
            raise ControlDenied("Seule la personne qui a demandé ce titre (ou une personne « DJ ») peut le passer.")
