"""Musique dans les salons vocaux (lecture par Lavalink).

Organisation, de la plus « pure » à la plus proche de Discord :

* ``models``, ``errors`` : pistes, entrées de la file, erreurs expliquées à l'utilisateur ;
* ``search`` : validation des recherches et des liens, sources prises en charge, normalisation ;
* ``queue`` : file d'attente FIFO bornée ;
* ``player`` : lecteur d'UN serveur (états et transitions) et gestionnaire des lecteurs ;
* ``permissions`` : qui peut faire quoi ;
* ``service`` : orchestration (seul module qui parle au moteur audio et à la base) ;
* ``lavalink`` : adaptateur vers Lavalink (wavelink), le seul module qui importe wavelink.

Tout, sauf ``lavalink``, se teste sans Discord et sans Lavalink. Les commandes sont dans
``discord_bot/cogs/music.py`` et l'affichage dans ``discord_bot/music_ui.py`` ; le SQL
de l'historique et des favoris est dans ``database/music.py``. Guide : docs/music.md.
"""
