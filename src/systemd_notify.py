"""Signes de vie envoyés à systemd (chien de garde ``WatchdogSec`` du service).

Si la boucle du bot se bloque, le fichier de vie n'est plus réécrit et systemd ne reçoit
plus ``WATCHDOG=1`` : il arrête puis relance le service. Hors systemd (Windows, terminal,
Docker), ``NOTIFY_SOCKET`` n'existe pas et rien n'est envoyé. Aucune dépendance externe :
le protocole est un simple datagramme sur une socket Unix.
"""

from __future__ import annotations

import os
import socket


def notify(state: str, env: dict | None = None) -> bool:
    """Envoie ``state`` (ex. ``WATCHDOG=1``) à systemd. Retourne ``True`` si le message est parti."""
    env = os.environ if env is None else env
    address = env.get("NOTIFY_SOCKET", "")
    if not address or not hasattr(socket, "AF_UNIX"):
        return False
    if address.startswith("@"):  # socket abstraite (Linux)
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(address)
            sock.sendall(state.encode("utf-8"))
    except OSError:
        return False
    return True
