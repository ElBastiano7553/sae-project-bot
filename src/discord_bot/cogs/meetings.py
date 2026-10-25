"""Gestionnaire de réunions (Samae 2.0, lot 1) : groupe /reunion et boutons du panneau.

Ce cog ne décide rien : il identifie la personne (et si elle est administratrice du
bot), appelle ``services.meetings`` qui applique les règles, puis met à jour le
panneau **sur place**. Les réponses aux commandes sont éphémères ; seuls le panneau
et le compte rendu final sont publics, et aucune mention ne notifie personne.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Callable

import discord
from discord import app_commands
from discord.ext import commands

from src.database.meetings import MeetingDetail
from src.discord_bot.bot import SaeBot, send_error
from src.discord_bot.embeds import error_embed
from src.discord_bot.help_ui import HelpView, collect_commands, render_pages
from src.discord_bot.meeting_ui import (
    MeetingButton,
    PagesView,
    meeting_label,
    meeting_list_embed,
    panel_embed,
    panel_view,
    progress_embed,
    report_embeds,
    success_embed,
)
from src.discord_bot.permissions import is_bot_admin
from src.discord_bot.views import ConfirmView
from src.services import text_limits as limits
from src.services.help import MEETING_PAGES
from src.services.meetings import (
    CANCELLED,
    DECISION,
    DISAGREEMENT,
    FINISHED,
    OPEN_STATUSES,
    RESERVE,
    RUNNING,
    STATUS_LABELS,
    Actor,
    MeetingError,
    StalePanel,
    parse_mentions,
    parse_steps,
)

log = logging.getLogger(__name__)

# Salon du compte rendu final (clés de config/server.toml, dans l'ordre de préférence) ;
# à défaut, le salon du panneau. « decisions » reste accepté pour les serveurs d'avant /refonte.
REPORT_CHANNEL_KEYS = ("comptes_rendus", "decisions")
# Pages publiées au plus pour un compte rendu (le reste reste lisible avec /reunion compte-rendu).
MAX_REPORT_MESSAGES = 10
NO_MENTIONS = discord.AllowedMentions.none()

LIST_CHOICES = [
    app_commands.Choice(name="Prévues ou en cours", value="ouvertes"),
    app_commands.Choice(name="Terminées", value="terminees"),
    app_commands.Choice(name="Annulées", value="annulees"),
    app_commands.Choice(name="Toutes", value="toutes"),
]
LIST_STATUSES = {"ouvertes": OPEN_STATUSES, "terminees": (FINISHED,), "annulees": (CANCELLED,), "toutes": ()}
HELP_TOPICS = [app_commands.Choice(name=page.title, value=page.key) for page in MEETING_PAGES]
STEP_START, STEP_NEXT, STEP_PREVIOUS = "demarrer", "suivante", "precedente"
STEP_CHOICES = [
    app_commands.Choice(name="Démarrer", value=STEP_START),
    app_commands.Choice(name="Étape suivante", value=STEP_NEXT),
    app_commands.Choice(name="Étape précédente", value=STEP_PREVIOUS),
]
NOTE_CHOICES = [
    app_commands.Choice(name="Réserve", value=RESERVE),
    app_commands.Choice(name="Désaccord", value=DISAGREEMENT),
]


async def meeting_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[int]]:
    bot: SaeBot = interaction.client  # type: ignore[assignment]
    meetings = bot.meetings.repo.list(limit=25, search=current.strip() or None, guild_id=interaction.guild_id)
    return [
        app_commands.Choice(name=limits.truncate(f"{meeting_label(m)} ({STATUS_LABELS[m.status]})", 100), value=m.id)
        for m in meetings
    ]


def _member_name(member: discord.abc.User) -> str:
    return getattr(member, "display_name", None) or str(member)


class MeetingsCog(commands.Cog):
    reunion = app_commands.Group(
        name="reunion", description="Réunions : ordre du jour, décisions, actions et compte rendu", guild_only=True
    )
    # Ce qu'on note pendant la réunion : /reunion ajouter point|decision|reserve|action.
    ajouter = app_commands.Group(
        name="ajouter", description="Ajouter un point, une décision, une réserve ou une action", parent=reunion
    )

    def __init__(self, bot: SaeBot) -> None:
        self.bot = bot
        self._panel_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def cog_load(self) -> None:
        # Boutons persistants : reconnus d'après leur identifiant, même après un redémarrage.
        self.bot.add_dynamic_items(MeetingButton)

    async def cog_unload(self) -> None:
        self.bot.remove_dynamic_items(MeetingButton)

    @property
    def service(self):
        return self.bot.meetings

    # --- Outils ------------------------------------------------------------------

    def _actor(self, interaction: discord.Interaction) -> Actor:
        user = interaction.user
        admin = isinstance(user, discord.Member) and is_bot_admin(user, self.bot.settings.admin_role_name)
        # Le serveur de l'interaction : une réunion d'un autre serveur est inaccessible (comme inexistante).
        return Actor(user.id, _member_name(user)[:100], admin, interaction.guild_id)

    def _channel_id(self, interaction: discord.Interaction) -> int | None:
        return getattr(interaction, "channel_id", None)

    def _message_url(self, guild_id: int | None, channel_id: int | None, message_id: int | None) -> str | None:
        if not guild_id or not channel_id or not message_id:
            return None
        return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"

    def _report_url(self, detail: MeetingDetail) -> str | None:
        ids = detail.meeting.report_messages
        return self._message_url(detail.meeting.guild_id, detail.meeting.report_channel_id, ids[0]) if ids else None

    async def _fail(self, interaction: discord.Interaction, exc: MeetingError) -> None:
        await send_error(interaction, str(exc), title=exc.title, tone="warning")

    async def _channel(self, channel_id: int):
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except discord.HTTPException:
                log.warning("Salon %s inaccessible pour une réunion", channel_id)
                return None
        return channel if hasattr(channel, "send") and hasattr(channel, "get_partial_message") else None

    async def _apply(self, interaction: discord.Interaction, operation: Callable[[], MeetingDetail], message: str) -> MeetingDetail | None:
        """Modification lancée par une commande : réponse éphémère, puis panneau actualisé sur place."""
        try:
            detail = operation()
        except MeetingError as exc:
            await self._fail(interaction, exc)
            return None
        panel = self._message_url(detail.meeting.guild_id, detail.meeting.channel_id, detail.meeting.panel_message_id)
        suffix = f"\n[Voir le panneau]({panel})" if panel else ""
        await interaction.response.send_message(
            embed=success_embed(f"{message} (réunion {meeting_label(detail.meeting)}).{suffix}"), ephemeral=True
        )
        await self.refresh_panel(detail)
        return detail

    def _resolve(self, interaction: discord.Interaction, reunion: int | None) -> MeetingDetail:
        return self.service.resolve(reunion, self._actor(interaction), self._channel_id(interaction))

    # --- Panneau -------------------------------------------------------------------

    async def publish_panel(self, detail: MeetingDetail, channel) -> discord.Message | None:
        try:
            message = await channel.send(embed=panel_embed(detail), view=panel_view(detail), allowed_mentions=NO_MENTIONS)
        except discord.HTTPException as exc:
            log.warning("Panneau de la réunion %s impossible à publier : %s", detail.meeting.id, getattr(exc, "status", exc))
            return None
        self.service.set_panel(detail.meeting.id, channel.id, message.id)
        return message

    async def refresh_panel(self, detail: MeetingDetail) -> bool:
        """Modifie le panneau existant ; s'il a été supprimé (réunion ouverte), le republie une seule fois."""
        meeting_id = detail.meeting.id
        async with self._panel_locks[meeting_id]:  # un seul rafraîchissement à la fois par réunion
            detail = self.service.get(meeting_id)  # état le plus récent (une autre action a pu passer)
            m = detail.meeting
            if not m.channel_id or not m.panel_message_id:
                return False
            channel = await self._channel(m.channel_id)
            if channel is None:
                return False
            try:
                await channel.get_partial_message(m.panel_message_id).edit(
                    embed=panel_embed(detail, report_url=self._report_url(detail)),
                    view=panel_view(detail),
                    allowed_mentions=NO_MENTIONS,
                )
                return True
            except discord.NotFound:
                if m.status not in OPEN_STATUSES:
                    return False
                log.info("Panneau de la réunion %s supprimé : republication", meeting_id)
                return await self.publish_panel(detail, channel) is not None
            except discord.HTTPException as exc:
                log.warning("Panneau de la réunion %s non actualisé : %s", meeting_id, getattr(exc, "status", exc))
                return False

    # --- Compte rendu ---------------------------------------------------------------

    async def publish_report(self, detail: MeetingDetail) -> tuple[str | None, str | None]:
        """Publie le compte rendu figé (une fois). Retourne (lien, erreur)."""
        m = detail.meeting
        existing = m.report_messages
        if existing and m.report_channel_id:
            channel = await self._channel(m.report_channel_id)
            if channel is not None:
                try:
                    await channel.fetch_message(existing[0])
                    return self._message_url(m.guild_id, m.report_channel_id, existing[0]), None
                except discord.NotFound:
                    pass  # supprimé : on le republie
                except discord.HTTPException:
                    return None, "le salon du compte rendu est inaccessible"
        # Salon #comptes-rendus de l'espace d'équipe : seulement pour une réunion du serveur principal (jamais un
        # compte rendu publié sur un autre serveur que celui de la réunion) ; sinon, le salon de la réunion.
        team_space = m.guild_id is not None and self.bot.team_guild_error(m.guild_id) is None
        bindings = self.bot.bindings_repo.get_all() if team_space else {}
        channel_id = next((bindings[key] for key in REPORT_CHANNEL_KEYS if key in bindings), None) or m.channel_id
        channel = await self._channel(channel_id) if channel_id else None
        if channel is None:
            return None, "aucun salon disponible (associez #comptes-rendus avec /admin installer ou /refonte)"
        pages = report_embeds(self.service.report(m.id))
        ids: list[int] = []
        try:
            for page in pages[:MAX_REPORT_MESSAGES]:
                message = await channel.send(embed=page, allowed_mentions=NO_MENTIONS)
                ids.append(message.id)
        except discord.HTTPException as exc:
            log.warning("Compte rendu de la réunion %s non publié : %s", m.id, getattr(exc, "status", exc))
            if not ids:
                return None, "le bot ne peut pas écrire dans le salon du compte rendu"
        self.service.set_report_messages(m.id, channel.id, ids)
        note = None if len(pages) <= MAX_REPORT_MESSAGES else "compte rendu long : la suite est dans /reunion compte-rendu"
        return self._message_url(m.guild_id, channel.id, ids[0]), note

    async def show_report(self, interaction: discord.Interaction, meeting_id: int) -> None:
        pages = report_embeds(self.service.report(meeting_id, interaction.guild_id))
        if len(pages) == 1:
            await interaction.response.send_message(embed=pages[0], ephemeral=True)
            return
        view = PagesView(pages, interaction.user.id)
        view.original = interaction
        await interaction.response.send_message(embed=pages[0], view=view, ephemeral=True)

    # --- Clôture (avec confirmation) -------------------------------------------------

    async def ask_close(self, interaction: discord.Interaction, meeting_id: int, *, finish: bool) -> None:
        actor = self._actor(interaction)
        detail = self.service.get(meeting_id, interaction.guild_id)
        what = "terminer la réunion" if finish else "annuler la réunion"
        self.service.ensure_control(detail, actor, what, (RUNNING,) if finish else OPEN_STATUSES)
        done = sum(1 for s in detail.steps if s.status == "done")
        if finish:
            text = (
                f"**Terminer la réunion {meeting_label(detail.meeting)} ?**\n"
                f"Étapes traitées : {done}/{len(detail.steps)}. Le compte rendu sera figé puis publié ; "
                "la réunion ne pourra plus être modifiée. (Expiration : 2 minutes.)"
            )
        else:
            text = (
                f"**Annuler la réunion {meeting_label(detail.meeting)} ?**\n"
                "Son historique est conservé, mais elle ne pourra plus être démarrée ni modifiée. (Expiration : 2 minutes.)"
            )

        async def confirmed(confirm: discord.Interaction) -> None:
            await self._close(confirm, meeting_id, actor, finish=finish)

        view = ConfirmView(interaction.user.id, confirmed, command="/reunion terminer" if finish else "/reunion annuler")
        view.original = interaction
        await interaction.response.send_message(content=text, view=view, ephemeral=True)

    async def _close(self, interaction: discord.Interaction, meeting_id: int, actor: Actor, *, finish: bool) -> None:
        try:
            detail = (self.service.finish if finish else self.service.cancel)(meeting_id, actor)
        except MeetingError as exc:
            await interaction.edit_original_response(content=None, embed=error_embed(str(exc), title=exc.title, tone="warning"), view=None)
            return
        lines = [f"Réunion {meeting_label(detail.meeting)} {'terminée' if finish else 'annulée'}."]
        if finish:
            url, problem = await self.publish_report(self.service.get(meeting_id, interaction.guild_id))
            if url:
                lines.append(f"[Compte rendu publié]({url}).")
            if problem:
                lines.append(f"Attention, compte rendu : {problem}. Il reste consultable avec `/reunion compte-rendu`.")
        await self.refresh_panel(detail)
        await interaction.edit_original_response(content=None, embed=success_embed("\n".join(lines), "Réunion clôturée"), view=None)

    # --- Boutons du panneau ------------------------------------------------------------

    async def handle_button(self, interaction: discord.Interaction, meeting_id: int, action: str, revision: int) -> None:
        actor = self._actor(interaction)
        operations = {
            "join": lambda: self.service.join(meeting_id, actor),
            "leave": lambda: self.service.leave(meeting_id, actor),
            "start": lambda: self.service.start(meeting_id, actor, expected_revision=revision),
            "prev": lambda: self.service.previous_step(meeting_id, actor, expected_revision=revision),
            "next": lambda: self.service.next_step(meeting_id, actor, expected_revision=revision),
        }
        try:
            if action == "report":
                await self.show_report(interaction, meeting_id)
                return
            if action in ("finish", "cancel"):
                await self.ask_close(interaction, meeting_id, finish=action == "finish")
                return
            detail = operations[action]()
        except StalePanel as exc:
            await self._redraw(interaction, self.service.get(meeting_id, interaction.guild_id))
            await self._fail(interaction, exc)
            return
        except MeetingError as exc:
            await self._fail(interaction, exc)
            return
        await self._redraw(interaction, detail)
        message = getattr(interaction, "message", None)
        if message is None or message.id != detail.meeting.panel_message_id:
            await self.refresh_panel(detail)

    async def _redraw(self, interaction: discord.Interaction, detail: MeetingDetail) -> None:
        """Redessine le message dont le bouton a été cliqué (sans créer de nouveau message)."""
        try:
            await interaction.response.edit_message(
                embed=panel_embed(detail, report_url=self._report_url(detail)), view=panel_view(detail)
            )
        except discord.HTTPException:
            log.info("Panneau de la réunion %s : interaction expirée, actualisation directe", detail.meeting.id)
            await self.refresh_panel(detail)

    # --- Commandes ----------------------------------------------------------------------

    @reunion.command(name="creer", description="Créer une réunion et publier son panneau dans ce salon")
    @app_commands.describe(
        titre="Titre de la réunion",
        objectifs="Objectifs (facultatif)",
        etapes="Ordre du jour : étapes séparées par « ; » (ex. : Bilan ; Démo ; Plan)",
        responsable="Responsable (vous par défaut)",
        participants="Participants : mentionnez-les avec @ (facultatif)",
        duree="Durée prévue en minutes (facultatif)",
        description="Description (facultatif)",
    )
    async def creer(
        self,
        interaction: discord.Interaction,
        titre: app_commands.Range[str, 3, 100],
        objectifs: app_commands.Range[str, 1, 1000] | None = None,
        etapes: app_commands.Range[str, 1, 2000] | None = None,
        responsable: discord.Member | None = None,
        participants: app_commands.Range[str, 1, 1500] | None = None,
        duree: app_commands.Range[int, 5, 600] | None = None,
        description: app_commands.Range[str, 1, 1000] | None = None,
    ) -> None:
        actor = self._actor(interaction)
        if responsable is not None and responsable.bot:
            await send_error(interaction, "Le responsable doit être un membre de l'équipe, pas un bot.", tone="warning")
            return
        guild = interaction.guild
        invited = []
        for user_id in parse_mentions(participants):
            member = guild.get_member(user_id) if guild is not None else None
            if member is not None and member.bot:
                continue
            invited.append((user_id, _member_name(member)[:100] if member else f"membre {user_id}"))
        try:
            detail = self.service.create(
                actor,
                title=titre,
                description=description or "",
                objectives=objectifs or "",
                leader=(responsable.id, _member_name(responsable)[:100]) if responsable else None,
                participants=invited,
                steps=parse_steps(etapes),
                planned_minutes=duree,
                channel_id=self._channel_id(interaction),
            )
        except MeetingError as exc:
            await self._fail(interaction, exc)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        channel = interaction.channel
        message = await self.publish_panel(detail, channel) if channel is not None else None
        if message is None:
            text = (
                f"Réunion {meeting_label(detail.meeting)} créée, mais le panneau n'a pas pu être publié ici "
                "(permissions « Envoyer des messages » et « Intégrer des liens »). "
                "Utilisez `/reunion voir publier:True` dans un salon où le bot peut écrire."
            )
            await interaction.followup.send(embed=error_embed(text, title="Panneau non publié", tone="warning"), ephemeral=True)
            return
        url = self._message_url(interaction.guild_id, message.channel.id, message.id)
        link = f" [Voir le panneau]({url})" if url else ""
        await interaction.followup.send(
            embed=success_embed(f"Réunion {meeting_label(detail.meeting)} créée.{link}", "Réunion créée"),
            ephemeral=True,
        )

    @reunion.command(name="voir", description="Afficher une réunion (et, pour le responsable, déplacer son panneau ici)")
    @app_commands.describe(reunion="Réunion (par défaut : celle de ce salon)", publier="Republier le panneau dans ce salon (responsable ou admin)")
    @app_commands.autocomplete(reunion=meeting_autocomplete)
    async def voir(self, interaction: discord.Interaction, reunion: int | None = None, publier: bool = False) -> None:
        try:
            detail = self._resolve(interaction, reunion)
            if publier:
                self.service.ensure_control(detail, self._actor(interaction), "déplacer le panneau")
        except MeetingError as exc:
            await self._fail(interaction, exc)
            return
        if not publier:
            await interaction.response.send_message(
                embed=panel_embed(detail, report_url=self._report_url(detail)), view=panel_view(detail), ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        old = (detail.meeting.channel_id, detail.meeting.panel_message_id)
        message = await self.publish_panel(detail, interaction.channel) if interaction.channel is not None else None
        if message is None:
            await interaction.followup.send(
                embed=error_embed("Le bot ne peut pas publier dans ce salon.", tone="warning"), ephemeral=True
            )
            return
        # Nouveau panneau publié avant de retirer l'ancien : jamais deux panneaux actifs, jamais aucun.
        if old[0] and old[1] and old[1] != message.id:
            channel = await self._channel(old[0])
            if channel is not None:
                try:
                    await channel.get_partial_message(old[1]).delete()
                except discord.HTTPException:
                    log.info("Ancien panneau de la réunion %s non supprimé", detail.meeting.id)
        await interaction.followup.send(embed=success_embed("Panneau publié dans ce salon.", "Panneau déplacé"), ephemeral=True)

    @reunion.command(name="liste", description="Lister les réunions")
    @app_commands.describe(statut="Réunions à afficher (prévues ou en cours par défaut)")
    @app_commands.choices(statut=LIST_CHOICES)
    async def liste(self, interaction: discord.Interaction, statut: app_commands.Choice[str] | None = None) -> None:
        key = statut.value if statut else "ouvertes"
        meetings = self.service.repo.list(LIST_STATUSES[key], limit=20, guild_id=interaction.guild_id)
        label = statut.name if statut else LIST_CHOICES[0].name
        await interaction.response.send_message(embed=meeting_list_embed(meetings, f"Réunions · {label}"), ephemeral=True)

    @reunion.command(name="rejoindre", description="Rejoindre une réunion")
    @app_commands.autocomplete(reunion=meeting_autocomplete)
    async def rejoindre(self, interaction: discord.Interaction, reunion: int | None = None) -> None:
        actor = self._actor(interaction)
        await self._apply(interaction, lambda: self.service.join(self._resolve(interaction, reunion).meeting.id, actor), "Vous participez à la réunion")

    @reunion.command(name="quitter", description="Quitter une réunion")
    @app_commands.autocomplete(reunion=meeting_autocomplete)
    async def quitter(self, interaction: discord.Interaction, reunion: int | None = None) -> None:
        actor = self._actor(interaction)
        await self._apply(interaction, lambda: self.service.leave(self._resolve(interaction, reunion).meeting.id, actor), "Vous avez quitté la réunion")

    @reunion.command(name="inviter", description="Ajouter un participant (responsable ou admin)")
    @app_commands.describe(membre="Membre à ajouter")
    @app_commands.autocomplete(reunion=meeting_autocomplete)
    async def inviter(self, interaction: discord.Interaction, membre: discord.Member, reunion: int | None = None) -> None:
        if membre.bot:
            await send_error(interaction, "Un bot ne peut pas participer à une réunion.", tone="warning")
            return
        actor = self._actor(interaction)
        member = (membre.id, _member_name(membre)[:100])
        await self._apply(
            interaction,
            lambda: self.service.invite(self._resolve(interaction, reunion).meeting.id, actor, member),
            f"{membre.mention} ajouté(e)",
        )

    @reunion.command(name="animer", description="Démarrer la réunion ou changer d'étape (responsable ou admin)")
    @app_commands.describe(etape="Démarrer, étape suivante ou étape précédente", reunion="Réunion (par défaut : celle de ce salon)")
    @app_commands.choices(etape=STEP_CHOICES)
    @app_commands.autocomplete(reunion=meeting_autocomplete)
    async def animer(self, interaction: discord.Interaction, etape: app_commands.Choice[str], reunion: int | None = None) -> None:
        actor = self._actor(interaction)
        operations = {
            STEP_START: (self.service.start, "Réunion démarrée"),
            STEP_NEXT: (self.service.next_step, "Étape suivante"),
            STEP_PREVIOUS: (self.service.previous_step, "Retour à l'étape précédente"),
        }
        operation, label = operations[etape.value]
        await self._apply(interaction, lambda: operation(self._resolve(interaction, reunion).meeting.id, actor), label)

    @reunion.command(name="progression", description="Progression de la réunion : étapes, temps écoulé")
    @app_commands.autocomplete(reunion=meeting_autocomplete)
    async def progression(self, interaction: discord.Interaction, reunion: int | None = None) -> None:
        try:
            detail = self._resolve(interaction, reunion)
        except MeetingError as exc:
            await self._fail(interaction, exc)
            return
        await interaction.response.send_message(embed=progress_embed(detail), ephemeral=True)

    @ajouter.command(name="point", description="Ajouter un point à l'ordre du jour (participants)")
    @app_commands.describe(texte="Intitulé du point")
    @app_commands.autocomplete(reunion=meeting_autocomplete)
    async def point(self, interaction: discord.Interaction, texte: app_commands.Range[str, 2, 100], reunion: int | None = None) -> None:
        actor = self._actor(interaction)
        await self._apply(interaction, lambda: self.service.add_step(self._resolve(interaction, reunion).meeting.id, actor, texte), "Point ajouté")

    @ajouter.command(name="decision", description="Enregistrer une décision (responsable ou admin, réunion en cours)")
    @app_commands.describe(texte="La décision adoptée", motif="Motif de la décision (facultatif)")
    @app_commands.autocomplete(reunion=meeting_autocomplete)
    async def decision(
        self,
        interaction: discord.Interaction,
        texte: app_commands.Range[str, 3, 500],
        motif: app_commands.Range[str, 1, 300] | None = None,
        reunion: int | None = None,
    ) -> None:
        actor = self._actor(interaction)
        await self._apply(
            interaction,
            lambda: self.service.add_note(self._resolve(interaction, reunion).meeting.id, actor, DECISION, texte, motif),
            "Décision enregistrée",
        )

    @ajouter.command(name="reserve", description="Enregistrer une réserve ou un désaccord (participants, réunion en cours)")
    @app_commands.describe(texte="Votre réserve ou votre désaccord", type="Réserve (par défaut) ou désaccord")
    @app_commands.choices(type=NOTE_CHOICES)
    @app_commands.autocomplete(reunion=meeting_autocomplete)
    async def reserve(
        self,
        interaction: discord.Interaction,
        texte: app_commands.Range[str, 3, 500],
        type: app_commands.Choice[str] | None = None,
        reunion: int | None = None,
    ) -> None:
        actor = self._actor(interaction)
        kind = type.value if type else RESERVE
        await self._apply(
            interaction,
            lambda: self.service.add_note(self._resolve(interaction, reunion).meeting.id, actor, kind, texte),
            "Désaccord enregistré" if kind == DISAGREEMENT else "Réserve enregistrée",
        )

    @ajouter.command(name="action", description="Ajouter une action à effectuer, avec son responsable (réunion en cours)")
    @app_commands.describe(description="Ce qu'il faut faire", responsable="Qui s'en charge", echeance="Échéance (ex. : 2026-11-05 ou 05/11)")
    @app_commands.autocomplete(reunion=meeting_autocomplete)
    async def action(
        self,
        interaction: discord.Interaction,
        description: app_commands.Range[str, 3, 300],
        responsable: discord.Member,
        echeance: app_commands.Range[str, 3, 20] | None = None,
        reunion: int | None = None,
    ) -> None:
        actor = self._actor(interaction)
        assignee = (responsable.id, _member_name(responsable)[:100])
        await self._apply(
            interaction,
            lambda: self.service.add_action(self._resolve(interaction, reunion).meeting.id, actor, description, assignee, echeance),
            f"Action ajoutée pour {responsable.mention}",
        )

    @reunion.command(name="terminer", description="Terminer la réunion et publier le compte rendu (responsable ou admin)")
    @app_commands.autocomplete(reunion=meeting_autocomplete)
    async def terminer(self, interaction: discord.Interaction, reunion: int | None = None) -> None:
        try:
            await self.ask_close(interaction, self._resolve(interaction, reunion).meeting.id, finish=True)
        except MeetingError as exc:
            await self._fail(interaction, exc)

    @reunion.command(name="annuler", description="Annuler une réunion prévue ou en cours (responsable ou admin)")
    @app_commands.autocomplete(reunion=meeting_autocomplete)
    async def annuler(self, interaction: discord.Interaction, reunion: int | None = None) -> None:
        try:
            await self.ask_close(interaction, self._resolve(interaction, reunion).meeting.id, finish=False)
        except MeetingError as exc:
            await self._fail(interaction, exc)

    @reunion.command(name="aide", description="Guide des réunions : créer, participer, animer, décisions, compte rendu")
    @app_commands.describe(sujet="Ouvrir directement un sujet (Créer une réunion par défaut)")
    @app_commands.choices(sujet=HELP_TOPICS)
    async def aide(self, interaction: discord.Interaction, sujet: app_commands.Choice[str] | None = None) -> None:
        user = interaction.user
        admin = isinstance(user, discord.Member) and is_bot_admin(user, self.bot.settings.admin_role_name)
        pages = render_pages(MEETING_PAGES, commands=collect_commands(self.bot.tree, interaction.guild_id), admin=admin, heading="Aide des réunions")
        view = HelpView(pages, user.id, home_label="Début")
        if sujet is not None:
            view.go_to_section(sujet.value)
        view.original = interaction
        await interaction.response.send_message(embed=view.embed, view=view, ephemeral=True)

    @reunion.command(name="compte-rendu", description="Lire le compte rendu (aperçu si la réunion n'est pas terminée)")
    @app_commands.describe(reunion="Réunion", publier="Republier le compte rendu final s'il a été supprimé (responsable ou admin)")
    @app_commands.autocomplete(reunion=meeting_autocomplete)
    async def compte_rendu(self, interaction: discord.Interaction, reunion: int | None = None, publier: bool = False) -> None:
        try:
            detail = self._resolve(interaction, reunion)
            if publier:
                self.service.ensure_control(detail, self._actor(interaction), "publier le compte rendu", (FINISHED,))
        except MeetingError as exc:
            await self._fail(interaction, exc)
            return
        if not publier:
            await self.show_report(interaction, detail.meeting.id)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        url, problem = await self.publish_report(detail)
        if url is None:
            await interaction.followup.send(embed=error_embed(f"Compte rendu non publié : {problem}.", tone="warning"), ephemeral=True)
            return
        await self.refresh_panel(detail)
        text = f"[Compte rendu]({url})" + (f"\nAttention : {problem}" if problem else "")
        await interaction.followup.send(embed=success_embed(text, "Compte rendu publié"), ephemeral=True)


async def setup(bot: SaeBot) -> None:
    await bot.add_cog(MeetingsCog(bot))
