"""Groupe /musique : écouter de la musique à plusieurs dans un salon vocal (lecture par Lavalink).

Ce cog ne décide rien : il décrit la personne qui agit (salon vocal réel, rôles réels),
appelle ``music.service`` qui applique les règles, puis répond. Les réponses aux commandes
sont éphémères ; seul le panneau du lecteur est public (aucune mention ne notifie personne).

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
from src.music.search import SOURCES, format_duration
from src.music.service import MAX_VOLUME, MIN_VOLUME, MusicService

log = logging.getLogger(__name__)

SOURCE_CHOICES = [app_commands.Choice(name=source.label, value=key) for key, source in SOURCES.items()]
# Regroupe les actualisations du panneau (fin de piste + ajout + clic en rafale = une seule modification).
PANEL_DELAY_SECONDS = 1.0
HISTORY_LIMIT = 50


class MusicCog(commands.Cog):
    musique = app_commands.Group(
        name="musique", description="Musique dans un salon vocal : recherche, file, volume, historique, favoris", guild_only=True
    )

    def __init__(self, bot: SaeBot) -> None:
        self.bot = bot
        self._panel_tasks: dict[int, asyncio.Task] = {}
        self._panel_locks: dict[int, asyncio.Lock] = {}
        self.panel_delay = PANEL_DELAY_SECONDS
        if bot.music is not None:
            bot.music.on_change = self.schedule_panel

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(MusicButton)

    async def cog_unload(self) -> None:
        self.bot.remove_dynamic_items(MusicButton)
        for task in self._panel_tasks.values():
            task.cancel()
        self._panel_tasks.clear()

    # --- Outils ------------------------------------------------------------------

    def _service(self) -> MusicService:
        service = self.bot.music
        if service is None:
            if self.bot.settings.music_config_error:
                raise MusicUnavailable("La musique est désactivée : sa configuration est invalide (un administrateur peut lancer `/etat`).")
            raise MusicUnavailable("La musique n'est pas activée sur ce bot (MUSIC_ENABLED=false).")
        return service

    def _listener(self, interaction: discord.Interaction) -> Listener:
        """La personne qui agit, d'après Discord (jamais d'après le contenu d'un message ou d'un bouton)."""
        user = interaction.user
        # Membre du serveur (et non simple utilisateur) : il a des permissions et un état vocal dans CE serveur.
        member = user if interaction.guild is not None and hasattr(user, "guild_permissions") else None
        voice = getattr(member, "voice", None)
        channel = getattr(voice, "channel", None)
        humans = sum(1 for m in getattr(channel, "members", ()) if not m.bot) if channel is not None else 0
        dj_role = (self.bot.settings.music_dj_role_name or "").casefold()
        has_dj = bool(dj_role) and member is not None and any(role.name.casefold() == dj_role for role in member.roles)
        return Listener(
            user_id=user.id,
            name=(getattr(user, "display_name", None) or str(user))[:100],
            voice_channel_id=channel.id if channel is not None else None,
            is_admin=member is not None and is_bot_admin(member, self.bot.settings.admin_role_name),
            has_dj_role=has_dj,
            humans_in_channel=humans,
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
        if not (perms.view_channel and perms.connect and perms.speak):
            raise MusicError(f"Le bot n'a pas le droit de rejoindre ou de parler dans <#{channel.id}> (permissions « Se connecter » et « Parler »).")
        limit = getattr(channel, "user_limit", 0) or 0
        if limit and len(getattr(channel, "members", ())) >= limit and not perms.move_members:
            raise MusicError(f"Le salon <#{channel.id}> est plein : le bot ne peut pas le rejoindre.")

    async def _fail(self, interaction: discord.Interaction, exc: MusicError) -> None:
        await send_error(interaction, str(exc), title=exc.title, tone="warning")

    async def _done(self, interaction: discord.Interaction, text: str, title: str = "Musique") -> None:
        embed = success_embed(text, title)
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _pages(self, interaction: discord.Interaction, pages: list[discord.Embed]) -> None:
        send = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
        if len(pages) == 1:
            await send(embed=pages[0], ephemeral=True)
            return
        view = PagesView(pages, interaction.user.id)
        view.original = interaction
        await send(embed=pages[0], view=view, ephemeral=True)

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

    @musique.command(name="jouer", description="Chercher un titre (ou coller un lien) et l'ajouter à la file")
    @app_commands.describe(recherche="Titre, artiste ou lien (YouTube, SoundCloud…)", source="Où chercher (par défaut : la première source activée)")
    @app_commands.choices(source=SOURCE_CHOICES)
    async def play(
        self,
        interaction: discord.Interaction,
        recherche: app_commands.Range[str, 1, 500],
        source: app_commands.Choice[str] | None = None,
    ) -> None:
        try:
            service = self._service()
            service.require_available()
            listener = self._listener(interaction)
            service.policy.require_same_channel(service.players.get(interaction.guild_id), listener)
            self._check_bot_can_join(interaction, listener)
        except MusicError as exc:
            await self._fail(interaction, exc)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)  # la recherche peut prendre quelques secondes
        try:
            outcome = await service.search(interaction.guild_id, listener.user_id, recherche, source=source.value if source else None)
        except MusicError as exc:
            await self._fail(interaction, exc)
            return
        if outcome.query_kind != "search" or len(outcome.tracks) == 1:
            await self._enqueue(interaction, list(outcome.tracks), outcome=outcome)
            return

        async def picked(choice: discord.Interaction, track: Track) -> None:
            await self._enqueue(choice, [track], outcome=None, edit=True)

        view = TrackSelectView(listener.user_id, result_options(outcome.tracks), picked, placeholder="Choisir un titre")
        view.original = interaction
        await interaction.followup.send(embed=results_embed(outcome, recherche), view=view, ephemeral=True)

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
        if edit:
            await interaction.edit_original_response(content=None, embed=success_embed(text, "Ajouté à la file"), view=None)
        else:
            await self._done(interaction, text, "Ajouté à la file")

    @musique.command(name="pause", description="Mettre la lecture en pause")
    async def pause(self, interaction: discord.Interaction) -> None:
        await self._control(interaction, "pause")

    @musique.command(name="reprendre", description="Reprendre la lecture mise en pause")
    async def resume(self, interaction: discord.Interaction) -> None:
        await self._control(interaction, "resume")

    @musique.command(name="passer", description="Passer au titre suivant de la file")
    async def skip(self, interaction: discord.Interaction) -> None:
        await self._control(interaction, "skip")

    @musique.command(name="arreter", description="Arrêter la lecture et vider la file (l'historique et les favoris restent)")
    async def stop(self, interaction: discord.Interaction) -> None:
        await self._control(interaction, "stop")

    @musique.command(name="quitter", description="Arrêter la musique et faire quitter le salon vocal au bot")
    async def leave(self, interaction: discord.Interaction) -> None:
        await self._control(interaction, "leave")

    async def _control(
        self, interaction: discord.Interaction, action: str, *, session_id: str | None = None, entry_id: int | None = None
    ) -> str | None:
        """Exécute une action de contrôle (commande ou bouton). Retourne le texte de confirmation, ou None si refusée."""
        guild_id = interaction.guild_id
        try:
            service = self._service()
            listener = self._listener(interaction)
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True, thinking=session_id is None)
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

    @musique.command(name="encours", description="Titre en cours : source, durée, progression, demandeur")
    async def now_playing(self, interaction: discord.Interaction) -> None:
        try:
            service = self._service()
        except MusicError as exc:
            await self._fail(interaction, exc)
            return
        snapshot = service.snapshot(interaction.guild_id)
        if snapshot.current is None:
            await send_error(interaction, "Aucun titre n'est en cours de lecture.", title="Aucune lecture", tone="warning")
            return
        await interaction.response.send_message(embed=player_embed(snapshot), ephemeral=True)

    @musique.command(name="file", description="Titre en cours et titres en attente (pages)")
    async def queue(self, interaction: discord.Interaction) -> None:
        try:
            service = self._service()
        except MusicError as exc:
            await self._fail(interaction, exc)
            return
        await self._pages(interaction, queue_pages(service.snapshot(interaction.guild_id)))

    @musique.command(name="volume", description=f"Voir ou régler le volume ({MIN_VOLUME} à {MAX_VOLUME} %)")
    @app_commands.describe(valeur=f"Nouveau volume, de {MIN_VOLUME} à {MAX_VOLUME} (laisser vide pour voir le volume actuel)")
    async def volume(self, interaction: discord.Interaction, valeur: app_commands.Range[int, MIN_VOLUME, MAX_VOLUME] | None = None) -> None:
        try:
            service = self._service()
            if valeur is None:
                await self._done(interaction, f"Volume actuel : **{service.snapshot(interaction.guild_id).volume} %**.")
                return
            listener = self._listener(interaction)
            await interaction.response.defer(ephemeral=True, thinking=True)
            value = await service.set_volume(interaction.guild_id, listener, valeur)
        except MusicError as exc:
            await self._fail(interaction, exc)
            return
        await self._done(interaction, f"Volume réglé sur **{value} %**.")

    @musique.command(name="historique", description="Dernières lectures sur ce serveur (pages)")
    async def history(self, interaction: discord.Interaction) -> None:
        try:
            service = self._service()
        except MusicError as exc:
            await self._fail(interaction, exc)
            return
        rows, total = service.history(interaction.guild_id, limit=HISTORY_LIMIT)
        await self._pages(interaction, history_pages(rows, total))

    @musique.command(name="favori", description="Ajouter le titre en cours à vos favoris (ou l'en retirer)")
    @app_commands.describe(retirer="Numéro d'un favori à retirer (voir /musique favoris) au lieu du titre en cours")
    async def favorite(self, interaction: discord.Interaction, retirer: app_commands.Range[int, 1, 1000] | None = None) -> None:
        try:
            service = self._service()
            if retirer is not None:
                removed = service.remove_favorite_at(interaction.guild_id, interaction.user.id, retirer)
                await self._done(interaction, f"Retiré de vos favoris : **{removed.title}**.", "Favoris")
                return
            track, added = service.toggle_favorite(interaction.guild_id, interaction.user.id)
        except MusicError as exc:
            await self._fail(interaction, exc)
            return
        verb = "ajouté à" if added else "retiré de"
        await self._done(interaction, f"{track_line(track)} : {verb} vos favoris.", "Favoris")

    @musique.command(name="favoris", description="Vos favoris sur ce serveur, avec un menu pour en rejouer un")
    async def favorites(self, interaction: discord.Interaction) -> None:
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
        view.message = await interaction.followup.send(view=view, ephemeral=True, wait=True)

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
            await interaction.response.send_message(
                "Ce panneau appartient à une session de musique terminée : lancez `/musique jouer`.", ephemeral=True
            )
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
        command = {"pause": "toggle", "resume": "toggle", "skip": "skip", "stop": "stop"}.get(action)
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
        await self.bot.music.on_track_problem(guild_id, entry_id_of(payload.track), "piste bloquée", stuck=True)

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

    @commands.Cog.listener()
    async def on_wavelink_node_disconnected(self, payload) -> None:
        if self.bot.music is None:
            return
        count = await self.bot.music.reset_all("Connexion au moteur audio perdue : relancez la musique avec /musique jouer.")
        log.warning("Connexion à Lavalink perdue : %d lecteur(s) arrêté(s) ; nouvelle tentative de connexion automatique", count)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState) -> None:
        """Le bot a été déplacé ou déconnecté du salon vocal par quelqu'un d'autre."""
        if self.bot.music is None or self.bot.user is None or member.id != self.bot.user.id:
            return
        before_id = before.channel.id if before.channel else None
        after_id = after.channel.id if after.channel else None
        if before_id == after_id:
            return
        await self.bot.music.on_voice_moved(member.guild.id, after_id)


async def setup(bot: SaeBot) -> None:
    await bot.add_cog(MusicCog(bot))
