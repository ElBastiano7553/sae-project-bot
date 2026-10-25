"""Commandes de musique : /play, /pause, /skip… dans un salon vocal (lecture par Lavalink).

Les commandes sont au premier niveau, comme sur les bots de musique habituels (voir le tableau
de correspondance avec l'ancien groupe /musique dans docs/music.md). Toutes s'utilisent sur un
serveur, jamais en message privé.

Ce cog ne décide rien : il décrit la personne qui agit (salon vocal réel, rôles réels),
appelle ``music.service`` qui applique les règles, puis répond. Les commandes slash et les
boutons du panneau passent par les mêmes méthodes du service (``_control``) : une seule
implémentation de la lecture, de la file et des permissions.

Les réponses sont éphémères ; seul le panneau du lecteur est public (aucune mention ne notifie
personne), et il est modifié sur place au lieu d'être republié.

Sans MUSIC_ENABLED=true, ou tant que Lavalink n'est pas joignable, chaque commande répond
par une explication : le reste du bot n'est jamais affecté. Guide : docs/music.md.
"""

from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

from src.discord_bot.bot import SaeBot, send_error
from src.discord_bot.embeds import success_embed
from src.discord_bot.meeting_ui import PagesView
from src.discord_bot.music_ui import (
    NO_MENTIONS,
    MusicButton,
    TrackSelectView,
    audio_report_embed,
    enqueue_text,
    favorites_pages,
    history_pages,
    player_embed,
    player_view,
    queue_pages,
    result_options,
    results_embed,
    track_line,
)
from src.discord_bot.permissions import is_bot_admin
from src.music.errors import MusicError, MusicUnavailable
from src.music.models import Track
from src.music.permissions import Listener
from src.music.player import LOOP_LABELS, LoopMode
from src.music.search import SOURCES, format_duration
from src.music.service import MAX_VOLUME, MIN_VOLUME, MusicService

log = logging.getLogger(__name__)

SOURCE_CHOICES = [app_commands.Choice(name=source.label, value=key) for key, source in SOURCES.items()]
LOOP_CHOICES = [
    app_commands.Choice(name="Désactivée", value=LoopMode.OFF.value),
    app_commands.Choice(name="Titre en cours", value=LoopMode.TRACK.value),
    app_commands.Choice(name="Toute la file", value=LoopMode.QUEUE.value),
]
# Regroupe les actualisations du panneau (fin de piste + ajout + clic en rafale = une seule modification).
PANEL_DELAY_SECONDS = 1.0
# Vérification du son après le lancement d'un titre (Lavalink envoie l'état du lecteur toutes les 5 s).
AUDIO_CHECK_SECONDS = 20.0
HISTORY_LIMIT = 50
# Noms des commandes de ce cog : vérifiés contre les autres cogs par les tests (aucun doublon).
COMMAND_NAMES = (
    "play", "search", "pause", "resume", "skip", "stop", "leave", "join", "nowplaying", "queue",
    "volume", "shuffle", "remove", "clear", "loop", "history", "favorite", "audiocheck",
)


class MusicCog(commands.Cog):
    favorite = app_commands.Group(name="favorite", description="Vos titres favoris sur ce serveur", guild_only=True)

    def __init__(self, bot: SaeBot) -> None:
        self.bot = bot
        self._panel_tasks: dict[int, asyncio.Task] = {}
        self._panel_locks: dict[int, asyncio.Lock] = {}
        self._audio_tasks: dict[int, asyncio.Task] = {}
        self.panel_delay = PANEL_DELAY_SECONDS
        self.audio_check_delay = AUDIO_CHECK_SECONDS
        if bot.music is not None:
            bot.music.on_change = self.schedule_panel

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(MusicButton)

    async def cog_unload(self) -> None:
        self.bot.remove_dynamic_items(MusicButton)
        for task in [*self._panel_tasks.values(), *self._audio_tasks.values()]:
            task.cancel()
        self._panel_tasks.clear()
        self._audio_tasks.clear()

    # --- Outils ------------------------------------------------------------------

    def _service(self) -> MusicService:
        service = self.bot.music
        if service is None:
            if self.bot.settings.music_config_error:
                raise MusicUnavailable("La musique est désactivée : sa configuration est invalide (un administrateur peut lancer `/etat`).")
            raise MusicUnavailable(
                "La musique n'est pas activée sur ce bot : la personne qui l'héberge doit mettre MUSIC_ENABLED=true dans .env "
                "et lancer Lavalink (docs/music.md)."
            )
        return service

    def _dj_role_id(self, guild_id: int | None) -> int | None:
        if guild_id is None:
            return None
        try:
            return self.bot.guild_settings_repo.get(guild_id).music_dj_role_id
        except Exception:
            log.exception("Réglages du serveur %s illisibles", guild_id)
            return None

    def _listener(self, interaction: discord.Interaction) -> Listener:
        """La personne qui agit, d'après Discord (jamais d'après le contenu d'un message ou d'un bouton)."""
        user = interaction.user
        # Membre du serveur (et non simple utilisateur) : il a des permissions et un état vocal dans CE serveur.
        member = user if interaction.guild is not None and hasattr(user, "guild_permissions") else None
        voice = getattr(member, "voice", None)
        channel = getattr(voice, "channel", None)
        humans = sum(1 for m in getattr(channel, "members", ()) if not m.bot) if channel is not None else 0
        roles = getattr(member, "roles", ()) if member is not None else ()
        # Rôle DJ de CE serveur (choisi depuis Discord par /setup role-dj), sinon le nom global MUSIC_DJ_ROLE_NAME.
        role_id = self._dj_role_id(interaction.guild_id)
        role_name = (self.bot.settings.music_dj_role_name or "").casefold()
        if role_id is not None:
            has_dj = any(getattr(role, "id", None) == role_id for role in roles)
        else:
            has_dj = bool(role_name) and any(role.name.casefold() == role_name for role in roles)
        return Listener(
            user_id=user.id,
            name=(getattr(user, "display_name", None) or str(user))[:100],
            voice_channel_id=channel.id if channel is not None else None,
            is_admin=member is not None and is_bot_admin(member, self.bot.settings.admin_role_name),
            has_dj_role=has_dj,
            humans_in_channel=humans,
            dj_role_configured=role_id is not None or bool(role_name),
        )

    def _check_bot_can_join(self, interaction: discord.Interaction, listener: Listener) -> None:
        guild = interaction.guild
        if guild is None or listener.voice_channel_id is None:
            return
        channel = guild.get_channel(listener.voice_channel_id)
        me = guild.me
        if channel is None or me is None or not hasattr(channel, "permissions_for"):
            return
        if me.voice is not None and me.voice.channel is not None and me.voice.channel.id == channel.id:
            return  # déjà dans le salon
        perms = channel.permissions_for(me)
        missing = [label for flag, label in (("view_channel", "Voir le salon"), ("connect", "Se connecter"), ("speak", "Parler"))
                   if not getattr(perms, flag, False)]
        if missing:
            raise MusicError(
                f"Le bot ne peut pas jouer dans <#{channel.id}> : il lui manque la permission {', '.join(f'« {m} »' for m in missing)}. "
                "Un administrateur peut la donner dans les paramètres du salon (rôle du bot)."
            )
        limit = getattr(channel, "user_limit", 0) or 0
        if limit and len(getattr(channel, "members", ())) >= limit and not perms.move_members:
            raise MusicError(f"Le salon <#{channel.id}> est plein : le bot ne peut pas le rejoindre.")

    async def _fail(self, interaction: discord.Interaction, exc: MusicError) -> None:
        await send_error(interaction, str(exc), title=exc.title, tone="warning")

    async def _send(self, interaction: discord.Interaction, **kwargs) -> None:
        """Réponse éphémère, que l'interaction ait déjà été différée ou non. Une interaction expirée est journalisée."""
        try:
            if interaction.response.is_done():
                await interaction.followup.send(ephemeral=True, **kwargs)
            else:
                await interaction.response.send_message(ephemeral=True, **kwargs)
        except discord.NotFound:
            log.warning("Réponse de musique perdue : l'interaction a expiré (plus de 15 minutes, ou délai de 3 s dépassé)")
        except discord.HTTPException as exc:
            log.warning("Réponse de musique impossible (HTTP %s)", getattr(exc, "status", "?"))

    async def _done(self, interaction: discord.Interaction, text: str, title: str = "Musique") -> None:
        await self._send(interaction, embed=success_embed(text, title))

    async def _pages(self, interaction: discord.Interaction, pages: list[discord.Embed]) -> None:
        if len(pages) == 1:
            await self._send(interaction, embed=pages[0])
            return
        view = PagesView(pages, interaction.user.id)
        view.original = interaction
        await self._send(interaction, embed=pages[0], view=view)

    async def _defer(self, interaction: discord.Interaction, *, thinking: bool = True) -> None:
        """Diffère la réponse (affiche « réfléchit… ») : obligatoire avant toute attente de Lavalink (limite de 3 s)."""
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=thinking)

    # --- Panneau du lecteur ---------------------------------------------------------------

    def schedule_panel(self, guild_id: int) -> None:
        """Appelé par le service après chaque changement : actualise le panneau peu après (une fois par rafale)."""
        task = self._panel_tasks.get(guild_id)
        if task is not None and not task.done():
            return
        try:
            task = asyncio.get_running_loop().create_task(self._refresh_later(guild_id), name=f"music-panel-{guild_id}")
        except RuntimeError:
            return  # pas de boucle (arrêt en cours)
        self._panel_tasks[guild_id] = task
        self._schedule_audio_check(guild_id)

    async def _refresh_later(self, guild_id: int) -> None:
        try:
            await asyncio.sleep(self.panel_delay)
            await self.refresh_panel(guild_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Panneau de musique non actualisé (serveur %s)", guild_id)
        finally:
            self._panel_tasks.pop(guild_id, None)

    def _schedule_audio_check(self, guild_id: int) -> None:
        """Après le lancement d'un titre, vérifie que le son part bien (sinon : cause affichée sur le panneau)."""
        service = self.bot.music
        player = service.players.get(guild_id) if service is not None else None
        if player is None or player.current is None or self.audio_check_delay <= 0:
            return
        entry_id = player.current.entry_id
        task = self._audio_tasks.get(guild_id)
        if task is not None and not task.done() and getattr(task, "entry_id", None) == entry_id:
            return
        if task is not None and not task.done():
            task.cancel()

        async def check() -> None:
            try:
                await asyncio.sleep(self.audio_check_delay)
                if self.bot.music is not None:
                    self.bot.music.check_audio(guild_id, entry_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Vérification du son impossible (serveur %s)", guild_id)

        task = asyncio.get_running_loop().create_task(check(), name=f"music-audio-{guild_id}")
        task.entry_id = entry_id  # type: ignore[attr-defined]
        self._audio_tasks[guild_id] = task

    async def refresh_panel(self, guild_id: int) -> bool:
        """Modifie le panneau sur place ; le publie s'il n'existe pas (ou a été supprimé) et qu'une session est en cours."""
        service = self.bot.music
        if service is None:
            return False
        player = service.players.get(guild_id)
        if player is None or player.text_channel_id is None:
            return False
        lock = self._panel_locks.setdefault(guild_id, asyncio.Lock())
        async with lock:
            snapshot = service.snapshot(guild_id)
            channel = self.bot.get_channel(player.text_channel_id)
            if channel is None or not hasattr(channel, "send") or getattr(getattr(channel, "guild", None), "id", guild_id) != guild_id:
                return False
            embed, view = player_embed(snapshot), player_view(snapshot)
            if player.panel_message_id:
                try:
                    await channel.get_partial_message(player.panel_message_id).edit(embed=embed, view=view, allowed_mentions=NO_MENTIONS)
                    if not snapshot.session_id:
                        player.panel_message_id = None  # session terminée : le prochain lecteur aura son propre panneau
                    return True
                except discord.NotFound:
                    player.panel_message_id = None  # supprimé à la main : republié ci-dessous si besoin
                except discord.HTTPException as exc:
                    log.warning("Panneau de musique non modifié (serveur %s) : %s", guild_id, getattr(exc, "status", exc))
                    return False
            if not snapshot.session_id or (snapshot.current is None and not snapshot.upcoming):
                return False
            try:
                message = await channel.send(embed=embed, view=view, allowed_mentions=NO_MENTIONS)
            except discord.HTTPException as exc:
                log.warning("Panneau de musique non publié (serveur %s) : %s", guild_id, getattr(exc, "status", exc))
                return False
            if player.session_id == snapshot.session_id:
                player.panel_message_id = message.id
            return True

    # --- Lecture et recherche -------------------------------------------------------------

    async def _prepare(self, interaction: discord.Interaction) -> tuple[MusicService, Listener] | None:
        """Vérifications communes à /play et /search, AVANT de différer (réponse immédiate si refus)."""
        try:
            service = self._service()
            service.require_available()
            listener = self._listener(interaction)
            service.policy.require_same_channel(service.players.get(interaction.guild_id), listener)
            self._check_bot_can_join(interaction, listener)
        except MusicError as exc:
            await self._fail(interaction, exc)
            return None
        return service, listener

    @app_commands.command(name="play", description="Lire un titre : recherche (1er résultat) ou lien YouTube, SoundCloud…")
    @app_commands.guild_only()
    @app_commands.describe(query="Titre, artiste ou lien d'une source autorisée", source="Où chercher (par défaut : la première source activée)")
    @app_commands.choices(source=SOURCE_CHOICES)
    async def play(
        self,
        interaction: discord.Interaction,
        query: app_commands.Range[str, 1, 500],
        source: app_commands.Choice[str] | None = None,
    ) -> None:
        prepared = await self._prepare(interaction)
        if prepared is None:
            return
        service, listener = prepared
        await self._defer(interaction)  # la recherche peut prendre quelques secondes
        try:
            outcome = await service.search(interaction.guild_id, listener.user_id, query, source=source.value if source else None)
        except MusicError as exc:
            await self._fail(interaction, exc)
            return
        # Texte : le premier résultat est lu (comme sur les autres bots) ; /search propose un choix.
        tracks = list(outcome.tracks) if outcome.query_kind != "search" else [outcome.tracks[0]]
        await self._enqueue(interaction, tracks, outcome=outcome if outcome.query_kind != "search" else None)

    @app_commands.command(name="search", description="Chercher des titres et choisir lequel ajouter à la file")
    @app_commands.guild_only()
    @app_commands.describe(query="Titre ou artiste", source="Où chercher (par défaut : la première source activée)")
    @app_commands.choices(source=SOURCE_CHOICES)
    async def search(
        self,
        interaction: discord.Interaction,
        query: app_commands.Range[str, 1, 200],
        source: app_commands.Choice[str] | None = None,
    ) -> None:
        prepared = await self._prepare(interaction)
        if prepared is None:
            return
        service, listener = prepared
        await self._defer(interaction)
        try:
            outcome = await service.search(interaction.guild_id, listener.user_id, query, source=source.value if source else None)
        except MusicError as exc:
            await self._fail(interaction, exc)
            return
        if outcome.query_kind != "search":  # un lien n'a rien à choisir
            await self._enqueue(interaction, list(outcome.tracks), outcome=outcome)
            return

        async def picked(choice: discord.Interaction, track: Track) -> None:
            await self._enqueue(choice, [track], outcome=None, edit=True)

        view = TrackSelectView(listener.user_id, result_options(outcome.tracks), picked, placeholder="Choisir un titre à ajouter")
        view.original = interaction
        await self._send(interaction, embed=results_embed(outcome, query), view=view)

    async def _enqueue(self, interaction: discord.Interaction, tracks: list[Track], *, outcome, edit: bool = False) -> None:
        """Ajoute à la file. Les droits sont revérifiés ici (la personne a pu changer de salon pendant le choix)."""
        try:
            service = self._service()
            listener = self._listener(interaction)
            self._check_bot_can_join(interaction, listener)
            result = await service.enqueue(interaction.guild_id, listener, tracks, text_channel_id=interaction.channel_id)
        except MusicError as exc:
            if edit:
                await interaction.edit_original_response(content=None, embed=None, view=None)
            await self._fail(interaction, exc)
            return
        text = enqueue_text(outcome, result.added, result.first_position, result.not_added)
        if result.started is not None:
            text += self._voice_warning(interaction)
        if edit:
            await interaction.edit_original_response(content=None, embed=success_embed(text, "Ajouté à la file"), view=None)
        else:
            await self._done(interaction, text, "Ajouté à la file")

    def _voice_warning(self, interaction: discord.Interaction) -> str:
        """Ce que Discord dit de l'état vocal du bot : rendu muet par un modérateur, ou spectateur d'une scène."""
        me = getattr(interaction.guild, "me", None)
        voice = getattr(me, "voice", None)
        if voice is None:
            return ""
        if getattr(voice, "mute", False):
            return "\n⚠️ Le bot est rendu **muet par le serveur** : personne ne l'entendra (clic droit sur le bot > « Rendre muet » à décocher)."
        if getattr(voice, "suppress", False):
            return "\n⚠️ Dans une scène, le bot est **spectateur** : un modérateur doit l'inviter à parler."
        return ""

    # --- Contrôles (commandes ET boutons : même chemin) ------------------------------------

    @app_commands.command(name="pause", description="Mettre la lecture en pause")
    @app_commands.guild_only()
    async def pause(self, interaction: discord.Interaction) -> None:
        await self._control(interaction, "pause")

    @app_commands.command(name="resume", description="Reprendre la lecture mise en pause")
    @app_commands.guild_only()
    async def resume(self, interaction: discord.Interaction) -> None:
        await self._control(interaction, "resume")

    @app_commands.command(name="skip", description="Passer au titre suivant de la file")
    @app_commands.guild_only()
    async def skip(self, interaction: discord.Interaction) -> None:
        await self._control(interaction, "skip")

    @app_commands.command(name="stop", description="Arrêter la lecture et vider la file (le bot reste dans le salon)")
    @app_commands.guild_only()
    async def stop(self, interaction: discord.Interaction) -> None:
        await self._control(interaction, "stop")

    @app_commands.command(name="leave", description="Arrêter la musique et faire quitter le salon vocal au bot")
    @app_commands.guild_only()
    async def leave(self, interaction: discord.Interaction) -> None:
        await self._control(interaction, "leave")

    @app_commands.command(name="join", description="Faire venir le bot dans votre salon vocal (sans lancer de titre)")
    @app_commands.guild_only()
    async def join(self, interaction: discord.Interaction) -> None:
        try:
            service = self._service()
            service.require_available()
            listener = self._listener(interaction)
            service.policy.require_same_channel(service.players.get(interaction.guild_id), listener)
            self._check_bot_can_join(interaction, listener)
            await self._defer(interaction)
            joined = await service.join(interaction.guild_id, listener, text_channel_id=interaction.channel_id)
        except MusicError as exc:
            await self._fail(interaction, exc)
            return
        text = f"Le bot a rejoint <#{listener.voice_channel_id}>. Lancez un titre avec `/play`." if joined else "Le bot est déjà dans votre salon."
        await self._done(interaction, text + (self._voice_warning(interaction) if joined else ""))

    @app_commands.command(name="shuffle", description="Mélanger les titres en attente")
    @app_commands.guild_only()
    async def shuffle(self, interaction: discord.Interaction) -> None:
        await self._control(interaction, "shuffle")

    @app_commands.command(name="clear", description="Vider la file d'attente (le titre en cours continue)")
    @app_commands.guild_only()
    async def clear(self, interaction: discord.Interaction) -> None:
        await self._control(interaction, "clear")

    @app_commands.command(name="loop", description="Répéter le titre en cours, toute la file, ou rien")
    @app_commands.guild_only()
    @app_commands.describe(mode="Mode de répétition")
    @app_commands.choices(mode=LOOP_CHOICES)
    async def loop(self, interaction: discord.Interaction, mode: app_commands.Choice[str]) -> None:
        await self._control(interaction, "loop", value=mode.value)

    @app_commands.command(name="remove", description="Retirer un titre de la file d'attente")
    @app_commands.guild_only()
    @app_commands.describe(position="Position dans la file (voir /queue)")
    async def remove(self, interaction: discord.Interaction, position: app_commands.Range[int, 1, 500]) -> None:
        await self._control(interaction, "remove", value=position)

    @remove.autocomplete("position")
    async def _remove_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[int]]:
        service = self.bot.music
        if service is None or interaction.guild_id is None:
            return []
        typed = current.strip().casefold()
        choices = []
        for position, entry in enumerate(service.snapshot(interaction.guild_id).upcoming, start=1):
            label = f"{position}. {entry.track.title} — {entry.track.author}"[:100]
            if typed and typed not in label.casefold():
                continue
            choices.append(app_commands.Choice(name=label, value=position))
            if len(choices) == 25:
                break
        return choices

    @app_commands.command(name="volume", description=f"Voir ou régler le volume ({MIN_VOLUME} à {MAX_VOLUME} %)")
    @app_commands.guild_only()
    @app_commands.describe(level=f"Nouveau volume, de {MIN_VOLUME} à {MAX_VOLUME} (laisser vide pour voir le volume actuel)")
    async def volume(self, interaction: discord.Interaction, level: app_commands.Range[int, MIN_VOLUME, MAX_VOLUME] | None = None) -> None:
        if level is None:
            try:
                service = self._service()
            except MusicError as exc:
                await self._fail(interaction, exc)
                return
            await self._done(interaction, f"Volume actuel : **{service.snapshot(interaction.guild_id).volume} %**.")
            return
        await self._control(interaction, "volume", value=level)

    async def _control(
        self,
        interaction: discord.Interaction,
        action: str,
        *,
        value=None,
        session_id: str | None = None,
        entry_id: int | None = None,
    ) -> str | None:
        """Exécute une action de contrôle (commande ou bouton). Retourne le texte de confirmation, ou None si refusée."""
        guild_id = interaction.guild_id
        try:
            service = self._service()
            listener = self._listener(interaction)
            await self._defer(interaction, thinking=session_id is None)
            if action in ("pause", "resume"):
                changed = await service.set_paused(guild_id, listener, action == "pause", session_id=session_id, entry_id=entry_id)
                text = ("Lecture mise en pause." if action == "pause" else "Lecture reprise.") if changed else (
                    "La lecture était déjà en pause." if action == "pause" else "La lecture n'était pas en pause."
                )
            elif action == "toggle":
                paused = await service.toggle_pause(guild_id, listener, session_id=session_id, entry_id=entry_id)
                text = "Lecture mise en pause." if paused else "Lecture reprise."
            elif action == "skip":
                skipped, started = await service.skip(guild_id, listener, session_id=session_id, entry_id=entry_id)
                text = f"Passé : {track_line(skipped.track)}."
                text += f"\nMaintenant : {track_line(started.track)}." if started else "\nLa file est vide."
            elif action == "stop":
                cleared = await service.stop(guild_id, listener, session_id=session_id, entry_id=entry_id)
                text = f"Lecture arrêtée, {cleared} titre(s) retiré(s) de la file. L'historique et les favoris sont conservés."
            elif action == "leave":
                cleared = await service.leave(guild_id, listener)
                text = "Le bot a quitté le salon vocal." + (f" {cleared} titre(s) retiré(s) de la file." if cleared else "")
            elif action == "shuffle":
                count = await service.shuffle(guild_id, listener)
                text = f"File mélangée ({count} titres)."
            elif action == "clear":
                cleared = await service.clear(guild_id, listener)
                text = f"File vidée : {cleared} titre(s) retiré(s). Le titre en cours continue." if cleared else "La file était déjà vide."
            elif action == "remove":
                removed = await service.remove(guild_id, listener, int(value))
                text = f"Retiré de la file : {track_line(removed.track)}."
            elif action == "loop":
                if session_id is not None:
                    service._check_session(service._require_player(guild_id), session_id, None)
                mode = await service.set_loop(guild_id, listener, value)
                text = f"Répétition : **{LOOP_LABELS[mode]}**."
            elif action == "volume":
                level = await service.set_volume(guild_id, listener, int(value))
                text = f"Volume réglé sur **{level} %** (retenu pour ce serveur)."
            else:
                raise ValueError(action)
        except MusicError as exc:
            await self._fail(interaction, exc)
            if session_id is not None:
                self.schedule_panel(guild_id)  # panneau périmé ou refus : il est redessiné avec l'état réel
            return None
        await self._done(interaction, text)
        return text

    # --- Consultation ------------------------------------------------------------------

    @app_commands.command(name="nowplaying", description="Titre en cours : source, durée, progression, demandeur")
    @app_commands.guild_only()
    async def now_playing(self, interaction: discord.Interaction) -> None:
        try:
            service = self._service()
        except MusicError as exc:
            await self._fail(interaction, exc)
            return
        snapshot = service.snapshot(interaction.guild_id)
        if snapshot.current is None:
            await send_error(interaction, "Aucun titre n'est en cours de lecture. Lancez-en un avec `/play`.", title="Aucune lecture", tone="warning")
            return
        await self._send(interaction, embed=player_embed(snapshot))

    @app_commands.command(name="queue", description="Titre en cours et titres en attente (pages)")
    @app_commands.guild_only()
    async def queue(self, interaction: discord.Interaction) -> None:
        try:
            service = self._service()
        except MusicError as exc:
            await self._fail(interaction, exc)
            return
        await self._pages(interaction, queue_pages(service.snapshot(interaction.guild_id)))

    @app_commands.command(name="history", description="Dernières lectures sur ce serveur (pages)")
    @app_commands.guild_only()
    async def history(self, interaction: discord.Interaction) -> None:
        try:
            service = self._service()
        except MusicError as exc:
            await self._fail(interaction, exc)
            return
        rows, total = service.history(interaction.guild_id, limit=HISTORY_LIMIT)
        await self._pages(interaction, history_pages(rows, total))

    @app_commands.command(name="audiocheck", description="Pas de son ? Vérifier chaque étape, de la recherche à l'envoi du son")
    @app_commands.guild_only()
    async def audiocheck(self, interaction: discord.Interaction) -> None:
        try:
            service = self._service()
        except MusicError as exc:
            await self._fail(interaction, exc)
            return
        await self._defer(interaction)
        live = None
        player = service.players.get(interaction.guild_id)
        if service.available and player is not None and player.connected:
            try:
                live = await asyncio.wait_for(service.backend.live_voice_state(interaction.guild_id), 5)
            except Exception:
                log.warning("État du lecteur Lavalink illisible pour /audiocheck", exc_info=True)
        me = getattr(interaction.guild, "me", None)
        voice = getattr(me, "voice", None)
        bot_voice = {
            "channel_id": getattr(getattr(voice, "channel", None), "id", None),
            "server_mute": bool(getattr(voice, "mute", False)),
            "suppress": bool(getattr(voice, "suppress", False)),
        }
        embed = audio_report_embed(
            service.audio_report(interaction.guild_id),
            lavalink_ready=service.available,
            lavalink=getattr(service.backend, "info", None),
            lavalink_problems=service.lavalink_problems(),
            live=live,
            bot_voice=bot_voice,
            frames=service.audio.frames,
        )
        await self._send(interaction, embed=embed)

    # --- Favoris -------------------------------------------------------------------------

    @favorite.command(name="add", description="Ajouter le titre en cours à vos favoris")
    async def favorite_add(self, interaction: discord.Interaction) -> None:
        try:
            service = self._service()
            track, added = service.add_favorite(interaction.guild_id, interaction.user.id)
        except MusicError as exc:
            await self._fail(interaction, exc)
            return
        text = f"{track_line(track)} : ajouté à vos favoris." if added else f"{track_line(track)} est déjà dans vos favoris."
        await self._done(interaction, text, "Favoris")

    @favorite.command(name="remove", description="Retirer un titre de vos favoris")
    @app_commands.describe(number="Numéro du favori (voir /favorite list)")
    async def favorite_remove(self, interaction: discord.Interaction, number: app_commands.Range[int, 1, 1000]) -> None:
        try:
            removed = self._service().remove_favorite_at(interaction.guild_id, interaction.user.id, number)
        except MusicError as exc:
            await self._fail(interaction, exc)
            return
        await self._done(interaction, f"Retiré de vos favoris : **{removed.title}**.", "Favoris")

    @favorite_remove.autocomplete("number")
    async def _favorite_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[int]]:
        service = self.bot.music
        if service is None or interaction.guild_id is None:
            return []
        typed = current.strip().casefold()
        choices = []
        for number, row in enumerate(service.favorites(interaction.guild_id, interaction.user.id), start=1):
            label = f"{number}. {row.title} — {row.author or 'artiste inconnu'}"[:100]
            if typed and typed not in label.casefold():
                continue
            choices.append(app_commands.Choice(name=label, value=number))
            if len(choices) == 25:
                break
        return choices

    @favorite.command(name="list", description="Vos favoris sur ce serveur, avec un menu pour en rejouer un")
    async def favorite_list(self, interaction: discord.Interaction) -> None:
        try:
            service = self._service()
        except MusicError as exc:
            await self._fail(interaction, exc)
            return
        rows = service.favorites(interaction.guild_id, interaction.user.id)
        pages = favorites_pages(rows)
        if not rows:
            await self._pages(interaction, pages)
            return
        options = [
            (f"{n}. {row.title}", f"{row.author or 'artiste inconnu'} · {format_duration(row.length_ms)}", row.uri)
            for n, row in enumerate(rows[:25], start=1)
        ]

        async def replay(choice: discord.Interaction, uri: str) -> None:
            try:
                track = await service.resolve_uri(choice.guild_id, choice.user.id, uri)
            except MusicError as exc:
                await choice.edit_original_response(content=None, embed=None, view=None)
                await self._fail(choice, exc)
                return
            await self._enqueue(choice, [track], outcome=None, edit=True)

        await self._pages(interaction, pages)
        view = TrackSelectView(interaction.user.id, options, replay, placeholder="Rejouer un favori (25 premiers)", timeout=300)
        try:
            view.message = await interaction.followup.send(view=view, ephemeral=True, wait=True)
        except discord.HTTPException:
            log.warning("Menu des favoris non envoyé (interaction expirée ?)")

    # --- Boutons du panneau ------------------------------------------------------------------

    async def handle_button(self, interaction: discord.Interaction, guild_id: int, session_id: str, entry_id: int, action: str) -> None:
        if interaction.guild_id != guild_id:
            # Bouton recopié depuis un autre serveur : jamais de contrôle croisé.
            await interaction.response.send_message("Ce panneau n'appartient pas à ce serveur.", ephemeral=True)
            return
        try:
            service = self._service()
        except MusicError as exc:
            await self._fail(interaction, exc)
            return
        player = service.players.get(guild_id)
        if player is None or not player.matches(session_id):
            await interaction.response.send_message("Ce panneau appartient à une session de musique terminée : lancez `/play`.", ephemeral=True)
            await self._disable_old_panel(interaction)
            return
        if action == "queue":
            await self._pages(interaction, queue_pages(service.snapshot(guild_id)))
            return
        if action == "favorite":
            try:
                track, added = service.toggle_favorite(guild_id, interaction.user.id, session_id=session_id, entry_id=entry_id)
            except MusicError as exc:
                await self._fail(interaction, exc)
                self.schedule_panel(guild_id)
                return
            verb = "ajouté à" if added else "retiré de"
            await self._done(interaction, f"{track_line(track)} : {verb} vos favoris.", "Favoris")
            return
        command = {"pause": "toggle", "resume": "toggle", "skip": "skip", "stop": "stop", "loop": "loop"}.get(action)
        if command is None:
            await interaction.response.send_message("Bouton inconnu.", ephemeral=True)
            return
        await self._control(interaction, command, session_id=session_id, entry_id=entry_id)

    async def _disable_old_panel(self, interaction: discord.Interaction) -> None:
        message = getattr(interaction, "message", None)
        if message is None:
            return
        try:
            await message.edit(view=None)
        except discord.HTTPException:
            log.debug("Impossible de retirer les boutons d'un ancien panneau de musique", exc_info=True)

    # --- Événements de Lavalink et de Discord ------------------------------------------------

    def _guild_of(self, player) -> int | None:
        guild = getattr(player, "guild", None)
        return getattr(guild, "id", None)

    @commands.Cog.listener()
    async def on_wavelink_track_start(self, payload) -> None:
        from src.music.lavalink import entry_id_of

        guild_id = self._guild_of(payload.player)
        if self.bot.music is None or guild_id is None:
            return
        self.bot.music.on_track_started(guild_id, entry_id_of(payload.track))

    @commands.Cog.listener()
    async def on_wavelink_player_update(self, payload) -> None:
        guild_id = self._guild_of(payload.player)
        if self.bot.music is None or guild_id is None:
            return
        self.bot.music.on_player_update(
            guild_id, connected=bool(payload.connected), ping_ms=int(payload.ping), position_ms=int(payload.position)
        )

    @commands.Cog.listener()
    async def on_wavelink_websocket_closed(self, payload) -> None:
        guild_id = self._guild_of(payload.player)
        if self.bot.music is None or guild_id is None:
            return
        self.bot.music.on_voice_closed(guild_id, int(payload.code), str(payload.reason or ""), bool(payload.by_remote))

    @commands.Cog.listener()
    async def on_wavelink_stats_update(self, payload) -> None:
        frames = getattr(payload, "frames", None)
        if self.bot.music is not None and frames is not None:
            self.bot.music.audio.frames = (int(frames.sent), int(frames.nulled), int(frames.deficit))

    @commands.Cog.listener()
    async def on_wavelink_track_end(self, payload) -> None:
        from src.music.lavalink import entry_id_of

        guild_id = self._guild_of(payload.player)
        if self.bot.music is None or guild_id is None:
            return
        await self.bot.music.on_track_end(guild_id, entry_id_of(payload.track), str(payload.reason))

    @commands.Cog.listener()
    async def on_wavelink_track_exception(self, payload) -> None:
        from src.music.lavalink import entry_id_of

        guild_id = self._guild_of(payload.player)
        if self.bot.music is None or guild_id is None:
            return
        exception = payload.exception if isinstance(payload.exception, dict) else {}
        message = f"piste illisible ({exception.get('severity', '?')}) : {str(exception.get('message', ''))[:200]}"
        await self.bot.music.on_track_problem(guild_id, entry_id_of(payload.track), message, stuck=False)

    @commands.Cog.listener()
    async def on_wavelink_track_stuck(self, payload) -> None:
        from src.music.lavalink import entry_id_of

        guild_id = self._guild_of(payload.player)
        if self.bot.music is None or guild_id is None:
            return
        await self.bot.music.on_track_problem(guild_id, entry_id_of(payload.track), "piste bloquée (aucune trame envoyée)", stuck=True)

    @commands.Cog.listener()
    async def on_wavelink_inactive_player(self, player) -> None:
        """Bot inactif (rien joué pendant MUSIC_IDLE_MINUTES, ou seul dans le salon) : il s'en va."""
        guild_id = self._guild_of(player)
        service = self.bot.music
        if service is None or guild_id is None:
            return
        state = service.players.get(guild_id)
        if state is None or not state.connected:
            try:
                await player.disconnect()
            except Exception:
                log.debug("Lecteur inactif inconnu déjà déconnecté", exc_info=True)
            return
        log.info("Musique : départ automatique du salon vocal après inactivité (serveur %s)", guild_id)
        try:
            await service.leave(guild_id, None)
        except MusicError:
            pass

    @commands.Cog.listener()
    async def on_wavelink_node_ready(self, payload) -> None:
        log.info("Lavalink connecté (session %s)", "reprise" if getattr(payload, "resumed", False) else "nouvelle")
        backend = self.bot.music_backend
        if backend is None or not hasattr(backend, "refresh_info"):
            return
        info = await backend.refresh_info()
        if info is None:
            return
        log.info("Lavalink %s : sources %s ; plugins %s", info.version, ", ".join(info.source_managers) or "aucune",
                 ", ".join(info.plugins) or "aucun")
        for problem in self.bot.music.lavalink_problems() if self.bot.music is not None else ():
            log.error("Musique : %s", problem)

    @commands.Cog.listener()
    async def on_wavelink_node_disconnected(self, payload) -> None:
        if self.bot.music is None:
            return
        count = await self.bot.music.reset_all("Connexion au moteur audio perdue : relancez la musique avec /play.")
        log.warning("Connexion à Lavalink perdue : %d lecteur(s) arrêté(s) ; nouvelle tentative de connexion automatique", count)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState) -> None:
        """Le bot a été déplacé, déconnecté ou rendu muet par quelqu'un d'autre."""
        if self.bot.music is None or self.bot.user is None or member.id != self.bot.user.id:
            return
        before_id = before.channel.id if before.channel else None
        after_id = after.channel.id if after.channel else None
        if after_id is not None and getattr(after, "mute", False) and not getattr(before, "mute", False):
            log.warning("Musique [serveur %s] : le bot a été rendu muet par le serveur, personne ne l'entendra", member.guild.id)
            player = self.bot.music.players.get(member.guild.id)
            if player is not None and player.connected:
                player.last_problem = "Le bot est rendu muet par le serveur : personne ne l'entend (à retirer dans le salon vocal)."
                self.schedule_panel(member.guild.id)
        if before_id == after_id:
            return
        await self.bot.music.on_voice_moved(member.guild.id, after_id)


async def setup(bot: SaeBot) -> None:
    await bot.add_cog(MusicCog(bot))
