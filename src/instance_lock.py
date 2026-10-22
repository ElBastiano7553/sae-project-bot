"""Une seule instance du bot à la fois sur une même base de données.

Deux instances connectées avec le même token répondent chacune aux commandes :
réponses en double, messages permanents modifiés deux fois, sauvegardes
concurrentes. C'est le cas typique d'un ``python -m src.main`` lancé à la main
alors que le service systemd tourne déjà.

Le verrou est un ``flock`` sur ``<dossier de la base>/samae.lock`` : le système le
libère dès que le processus s'arrête, même après un crash ou un ``kill -9``.
Aucun fichier « orphelin » ne peut donc bloquer un redémarrage. Sous Windows
(développement local), ``fcntl`` n'existe pas : le verrou est ignoré.
"""

from __future__ import annotations

import os
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None

LOCK_NAME = "samae.lock"


class AlreadyRunning(Exception):
    """Une autre instance détient déjà le verrou. Le message est destiné à l'utilisateur."""


class InstanceLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> bool:
        """Prend le verrou. Retourne ``False`` si le verrouillage n'est pas disponible (Windows).

        Lève ``AlreadyRunning`` si une autre instance tourne déjà.
        """
        if fcntl is None:
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            other = os.read(fd, 32).decode("ascii", "replace").strip() or "?"
            os.close(fd)
            raise AlreadyRunning(
                f"une autre instance du bot tourne déjà (processus {other}). "
                "Arrêtez-la d'abord : `sudo systemctl stop samae` pour le service, Ctrl+C dans un terminal."
            ) from None
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode("ascii"))
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None
