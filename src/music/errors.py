"""Erreurs de la musique : chacune porte un message clair, affiché tel quel à l'utilisateur."""

from __future__ import annotations


class MusicError(Exception):
    """Action refusée ou impossible. ``str(exc)`` est destiné à l'utilisateur, jamais un détail technique."""

    title = "Musique"


class MusicUnavailable(MusicError):
    title = "Musique indisponible"


class NotInVoice(MusicError):
    title = "Salon vocal requis"


class WrongVoiceChannel(MusicError):
    title = "Autre salon vocal"


class NothingPlaying(MusicError):
    title = "Aucune lecture"


class InvalidQuery(MusicError):
    title = "Recherche refusée"


class NoResults(MusicError):
    title = "Aucun résultat"


class SourceUnavailable(MusicError):
    title = "Source indisponible"


class QueueFull(MusicError):
    title = "File pleine"


class ControlDenied(MusicError):
    title = "Action réservée"


class StaleControl(MusicError):
    """Bouton d'un ancien lecteur, ou d'un titre qui n'est plus en cours."""

    title = "Lecteur périmé"


class SlowDown(MusicError):
    title = "Doucement"


class BackendError(Exception):
    """Erreur du moteur audio (Lavalink). Le message est technique : journalisé, jamais affiché tel quel."""
