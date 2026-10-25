"""Accueil des nouveaux membres et choix des rôles : /roles, bouton « Choisir mes rôles », message d'arrivée.

* ``/roles`` et le bouton persistant ouvrent un menu privé : chaque membre choisit
  lui-même ses domaines de travail et ses notifications, sans administrateur.
  Seuls les rôles du catalogue (``services.self_roles``) peuvent changer.
* Si ``WELCOME_ON_JOIN=true``, un message d'accueil (``content/accueil.toml``)
  est publié à chaque arrivée, puis supprimé après ``WELCOME_DELETE_AFTER_HOURS``
  heures (ou dès le départ du membre). Seuls ces messages sont supprimés.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging

import discord
from discord import app_commands
from discord.ext import commands

from src.config import load_link_catalog, load_planning
from src.discord_bot.bot import SaeBot, require_team_space
from src.discord_bot.embeds import payload_to_embed, payload_to_view
from src.discord_bot.roles_ui import RolePickerButton, RolePickerView, picker_embed, result_embed, send_problem
from src.services.content import render_message
from src.services.permanent_messages import GatewayError
from src.services.self_roles import SelfRoleCatalog, describe_change, plan_change
from src.services.welcome import (
    PENDING_KEY,
    PendingWelcome,
    delete_time,
    dump_pending,
    load_pending,
    load_welcome_template,
    personalize,
    split_due,
)

log = logging.getLogger(__name__)

# Vérification des messages d'accueil à supprimer.
CLEANUP_INTERVAL_SECONDS = 10 * 60
AUDIT_REASON = "Choix des rôles par le membre (Samae)"


class WelcomeCog(commands.Cog):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Espace d'équipe : utilisable seulement sur le serveur principal (voir /setup serveur-principal)."""
        return require_team_space(interaction)

    def __init__(self, bot: SaeBot) -> None:
        self.bot = bot
        self._cleanup_task: asyncio.Task | None = None
        # Un seul changement de rôles à la fois par membre (double clic, deux menus ouverts).
        self._locks: dict[int, asyncio.Lock] = {}

    async def cog_load(self) -> None:
        # Bouton persistant : reconnu d'après son identifiant, même après un redémarrage.
        self.bot.add_dynamic_items(RolePickerButton)
        if self.bot.settings.welcome_on_join and self.bot.settings.welcome_delete_hours:
            self._cleanup_task = asyncio.create_task(self._cleanup_loop(), name="welcome-cleanup")

    async def cog_unload(self) -> None:
        self.bot.remove_dynamic_items(RolePickerButton)
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()

    # --- Choix des rôles ------------------------------------------------------------------

    @app_commands.command(name="roles", description="Choisir mes domaines de travail et mes notifications")
    @app_commands.guild_only()
    async def roles(self, interaction: discord.Interaction) -> None:
        await self.open_picker(interaction)

    def _catalog(self, interaction: discord.Interaction) -> tuple[discord.Member | None, SelfRoleCatalog | None, str]:
        """Membre, catalogue des rôles et, si le choix est impossible, la raison à afficher."""
        member = interaction.user
        # Hors serveur, Discord transmet un utilisateur sans rôles (discord.User) au lieu d'un membre.
        problem = self.bot.team_guild_error(interaction.guild_id)
        if interaction.guild is None or problem or getattr(member, "roles", None) is None:
            return None, None, problem or "Le choix des rôles se fait sur le serveur principal du bot."
        catalog = self.bot.self_role_catalog()
        if catalog is None:
            return member, None, "Le serveur de l'équipe est momentanément inaccessible pour Samae. Réessayez dans un instant."
        if not catalog.bot_can_manage:
            return member, catalog, "Samae n'a pas la permission « Gérer les rôles ». Prévenez un administrateur (`/admin diagnostic`)."
        if not catalog.roles:
            return member, catalog, "Aucun rôle n'est proposé pour le moment. Un administrateur peut les créer avec `/refonte lancer`."
        return member, catalog, ""

    async def open_picker(self, interaction: discord.Interaction) -> None:
        member, catalog, problem = self._catalog(interaction)
        if problem or member is None or catalog is None:
            await send_problem(interaction, problem)
            return
        current = {role.id for role in member.roles}
        view = RolePickerView(self, catalog, member.id, current)
        view.original = interaction
        await interaction.response.send_message(embed=picker_embed(catalog, current), view=view, ephemeral=True)

    async def apply_choice(self, interaction: discord.Interaction, selected_ids: list[int], *, previous: RolePickerView | None = None) -> None:
        """Applique le choix du menu. Le catalogue est relu : un rôle devenu sensible entre-temps est refusé."""
        member, catalog, problem = self._catalog(interaction)
        if problem or member is None or catalog is None:
            await send_problem(interaction, problem)
            return
        lock = self._locks.setdefault(member.id, asyncio.Lock())
        if lock.locked():
            await send_problem(interaction, "Un changement de rôles est déjà en cours : patientez une seconde.", title="Patientez")
            return
        async with lock:
            await interaction.response.defer()
            current = {role.id for role in member.roles}
            change = plan_change(catalog, current, selected_ids)
            guild = interaction.guild
            try:
                add = [r for r in (guild.get_role(role.role_id) for role in change.add) if r is not None]
                remove = [r for r in (guild.get_role(role.role_id) for role in change.remove) if r is not None]
                if add:
                    await member.add_roles(*add, reason=AUDIT_REASON)
                if remove:
                    await member.remove_roles(*remove, reason=AUDIT_REASON)
            except discord.Forbidden:
                log.warning("Choix des rôles refusé par Discord pour %s (rôle de Samae trop bas ?)", member.id)
                await send_problem(
                    interaction,
                    "Discord a refusé le changement : le rôle de Samae doit être placé au-dessus des rôles proposés. "
                    "Prévenez un administrateur (`/admin diagnostic`).",
                )
                return
            except discord.HTTPException as exc:
                log.warning("Choix des rôles impossible (HTTP %s)", exc.status)
                await send_problem(interaction, "Discord n'a pas pu enregistrer le changement. Réessayez dans un instant.")
                return
            current = (current | {role.role_id for role in change.add}) - {role.role_id for role in change.remove}
            if not change.empty:
                log.info("Rôles du membre %s : +%d −%d", member.id, len(change.add), len(change.remove))
            if previous is not None:
                previous.stop()  # l'ancien menu est remplacé : son expiration ne doit pas retirer le nouveau
            view = RolePickerView(self, catalog, member.id, current)
            view.original = interaction
            await interaction.edit_original_response(
                embeds=[result_embed(describe_change(change), ok=not change.empty), picker_embed(catalog, current)], view=view
            )

    # --- Accueil des nouveaux membres ---------------------------------------------------------

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if not self.bot.settings.welcome_on_join or member.bot or member.guild.id != self.bot.home_guild_id:
            return
        try:
            await self.send_welcome(member)
        except Exception:  # un accueil raté ne doit jamais gêner le reste du bot
            log.exception("Message d'accueil non publié pour le membre %s", member.id)

    async def send_welcome(self, member: discord.Member) -> discord.Message | None:
        template = personalize(load_welcome_template(), member.mention)
        channel_id = self.bot.channel_id_for(template.channel_key)
        if channel_id is None:
            log.warning("Message d'accueil non publié : le salon « %s » n'est pas associé (/admin installer).", template.channel_key)
            return None
        channel = self.bot.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            log.warning("Message d'accueil non publié : salon %s introuvable ou non textuel.", channel_id)
            return None
        payload = render_message(
            template,
            load_link_catalog(),
            load_planning(),
            channels=self.bot.channel_mentions(),
            self_roles=self.bot.self_role_catalog(),
        )
        embed = payload_to_embed(payload)
        hours = self.bot.settings.welcome_delete_hours
        embed.set_footer(text=f"Ce message disparaîtra dans {hours} h." if hours else "Message d'accueil de Samae")
        view = payload_to_view(payload)
        # Seul le nouveau membre est notifié ; jamais @everyone ni un rôle.
        mentions = discord.AllowedMentions(everyone=False, roles=False, users=[member])
        try:
            if view is None:
                message = await channel.send(content=member.mention, embed=embed, allowed_mentions=mentions)
            else:
                message = await channel.send(content=member.mention, embed=embed, view=view, allowed_mentions=mentions)
        except discord.HTTPException as exc:
            log.warning("Message d'accueil refusé par Discord dans #%s (HTTP %s)", channel.name, exc.status)
            return None
        if hours:
            now = dt.datetime.now(dt.UTC)
            pending = load_pending(self.bot.state_repo.get(PENDING_KEY))
            pending.append(PendingWelcome(channel.id, message.id, member.id, delete_time(now, hours)))
            self.bot.state_repo.set(PENDING_KEY, dump_pending(pending))
        return message

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        """Un membre reparti avant la suppression : son message d'accueil est retiré tout de suite."""
        if not self.bot.settings.welcome_on_join or member.guild.id != self.bot.home_guild_id:
            return
        pending = load_pending(self.bot.state_repo.get(PENDING_KEY))
        if any(item.member_id == member.id for item in pending):
            await self.delete_due(member_id=member.id)

    async def delete_due(self, *, now: dt.datetime | None = None, member_id: int | None = None) -> int:
        """Supprime les messages d'accueil arrivés à échéance (ou ceux d'un membre). Retourne le nombre traité."""
        now = now or dt.datetime.now(dt.UTC)
        pending = load_pending(self.bot.state_repo.get(PENDING_KEY))
        if member_id is None:
            due, keep = split_due(pending, now)
        else:
            due = [item for item in pending if item.member_id == member_id]
            keep = [item for item in pending if item.member_id != member_id]
        for item in due:
            try:
                await self.bot.gateway.delete(item.channel_id, item.message_id)
            except GatewayError as exc:
                # On ne réessaie pas indéfiniment : le message reste, un administrateur peut le supprimer.
                log.warning("Message d'accueil %s non supprimé : %s", item.message_id, exc)
        if due:
            self.bot.state_repo.set(PENDING_KEY, dump_pending(keep))
        return len(due)

    async def _cleanup_loop(self) -> None:
        await self.bot.wait_until_ready()
        while True:
            try:
                await self.delete_due()
            except asyncio.CancelledError:
                raise
            except Exception:  # la boucle ne doit jamais s'arrêter
                log.exception("Erreur pendant la suppression des messages d'accueil")
            await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)


async def setup(bot: SaeBot) -> None:
    await bot.add_cog(WelcomeCog(bot))
