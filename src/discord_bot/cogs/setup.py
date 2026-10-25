"""Groupe /setup : configurer Samae depuis Discord, sur N'IMPORTE QUEL serveur où il est invité.

Aucun identifiant de serveur, de salon ou de rôle n'est à écrire dans le code ni dans .env :

* ``/setup etat`` : ce qui fonctionne sur ce serveur, ce qui manque et comment le régler ;
* ``/setup serveur-principal`` : fait de ce serveur le serveur principal (espace d'équipe : messages
  permanents, /refonte, /ia, /activite, /roles). Réservé au **propriétaire du bot** (compte qui a créé
  l'application sur le portail développeur) : l'administrateur d'un autre serveur ne peut jamais
  s'approprier l'espace d'équipe.

Chaque commande est protégée deux fois, comme /admin : permission par défaut « Gérer le serveur »
(le groupe est masqué dans Discord) et vérification côté bot à chaque appel.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from src.discord_bot.bot import SaeBot, send_error
from src.discord_bot.cogs.admin import admin_only
from src.discord_bot.embeds import add_field, make_embed, success_embed
from src.discord_bot.views import ConfirmView
from src.services.design import Tone

log = logging.getLogger(__name__)

# Permissions dont le bot a besoin dans un salon textuel.
TEXT_PERMISSIONS = {"send_messages": "Envoyer des messages", "embed_links": "Intégrer des liens"}


def _missing(perms, wanted: dict[str, str]) -> list[str]:
    return [label for flag, label in wanted.items() if not getattr(perms, flag, False)]


class SetupCog(commands.Cog):
    setup = app_commands.Group(
        name="setup",
        description="Configurer Samae sur ce serveur : état et serveur principal",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    def __init__(self, bot: SaeBot) -> None:
        self.bot = bot

    # --- État de ce serveur ---------------------------------------------------------------

    def status_lines(self, guild: discord.Guild) -> list[tuple[str, str]]:
        """(titre, texte) de chaque partie du bot pour CE serveur : rien d'un autre serveur, aucun secret."""
        bot, settings = self.bot, self.bot.settings
        me = guild.me
        sections: list[tuple[str, str]] = []

        sections.append(("📅 Réunions et 📁 projets", "✅ disponibles sur ce serveur (`/reunion creer`, `/projet liste`) ; données propres à ce serveur."))

        # Espace d'équipe
        home = bot.home_guild_id
        if home is None:
            team = (
                "▫️ aucun serveur principal (facultatif). Il sert aux messages permanents, à /refonte, /ia, /activite et /roles. "
                "Le propriétaire du bot peut choisir ce serveur avec `/setup serveur-principal`."
            )
        elif home == guild.id:
            problem = bot.team_guild_error(guild.id)
            source = bot.home_guild_source
            team = f"✅ ce serveur est le serveur principal ({source})." if problem is None else f"⚠️ {problem}"
            if problem is None and not bot.setup_confirmed_at:
                team += "\nÉtape suivante : `/admin installer` (associe les salons et publie les messages permanents)."
        else:
            team = "▫️ le serveur principal est un autre serveur : l'espace d'équipe n'est pas proposé ici."
        sections.append(("🏠 Espace d'équipe", team))

        if me is not None:
            unreadable = [c.mention for c in getattr(guild, "text_channels", ()) if _missing(c.permissions_for(me), {"view_channel": "Voir"})]
            text = "✅ le bot peut écrire dans ce salon." if not unreadable else f"Salons invisibles pour le bot : {len(unreadable)} (normal pour les salons privés)."
            sections.append(("🔐 Permissions", text))
        return sections

    @setup.command(name="etat", description="Ce qui fonctionne sur ce serveur, ce qui manque et comment le régler")
    @admin_only()
    async def status(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await send_error(interaction, "Cette commande s'utilise sur un serveur.")
            return
        embed = make_embed(f"⚙️ Samae sur {guild.name}", "Réglages de CE serveur. Les secrets restent dans le fichier .env de la machine.",
                           tone=Tone.INFO)
        for title, text in self.status_lines(guild):
            add_field(embed, title, text, inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # --- Serveur principal -----------------------------------------------------------------

    @setup.command(name="serveur-principal", description="Faire de ce serveur le serveur principal (propriétaire du bot seulement)")
    @app_commands.describe(
        reinitialiser="Oublier l'installation de l'espace d'équipe faite sur un autre serveur (rien n'y est supprimé)"
    )
    @admin_only()
    async def home(self, interaction: discord.Interaction, reinitialiser: bool = False) -> None:
        guild = interaction.guild
        bot = self.bot
        if guild is None:
            await send_error(interaction, "Cette commande s'utilise sur un serveur.")
            return
        if not await bot.is_owner(interaction.user):
            await send_error(
                interaction,
                "Seul le propriétaire du bot (le compte qui a créé l'application sur le portail développeur Discord) peut "
                "choisir le serveur principal.",
                title="Accès refusé",
            )
            return
        if bot.settings.guild_id and bot.settings.guild_id != guild.id:
            await send_error(
                interaction,
                f"Le serveur principal est fixé dans .env (DISCORD_GUILD_ID = {bot.settings.guild_id}). Videz DISCORD_GUILD_ID "
                "puis redémarrez le bot pour le choisir depuis Discord.",
                tone="warning",
            )
            return
        conflict = bot.team_space_conflict if bot.home_guild_id == guild.id else (
            bot.team_space_guild_id if bot.state_repo.get("setup_confirmed_at") and bot.team_space_guild_id not in (None, guild.id) else None
        )
        if conflict is not None and not reinitialiser:
            await send_error(
                interaction,
                f"L'espace d'équipe est installé sur un autre serveur ({conflict}). Relancez avec `reinitialiser:True` pour "
                "l'oublier et l'installer ici ensuite (`/admin installer`). Rien n'est supprimé sur l'autre serveur, mais le bot "
                "n'y actualisera plus ses messages permanents.",
                tone="warning",
            )
            return
        if bot.home_guild_id == guild.id and conflict is None:
            await interaction.response.send_message(
                embed=success_embed("Ce serveur est déjà le serveur principal.", "Serveur principal"), ephemeral=True
            )
            return
        text = (
            f"**Faire de « {guild.name} » le serveur principal ?**\n"
            "Les commandes de l'espace d'équipe (/admin, /refonte, /ia, /activite, /roles) y apparaîtront, et disparaîtront "
            "de l'ancien serveur principal s'il y en avait un. Les réunions et /projet ne changent pas."
            + ("\nL'installation faite sur l'autre serveur sera oubliée (rien n'y est supprimé)." if conflict is not None else "")
            + " (Expiration : 2 minutes.)"
        )

        async def confirmed(confirm: discord.Interaction) -> None:
            if conflict is not None:
                bot.reset_team_space()
            if not bot.settings.guild_id:
                bot.set_home_guild(guild.id)
            bot.record_admin_action(confirm, "setup serveur-principal", str(guild.id), "réinitialisé" if conflict else "")
            synced = await bot.sync_commands()
            note = "" if synced is not None else "\n⚠️ Commandes non synchronisées avec Discord : nouvel essai au prochain démarrage."
            await confirm.edit_original_response(
                content=None,
                embed=success_embed(f"« {guild.name} » est maintenant le serveur principal. Étape suivante : `/admin installer`.{note}",
                                    "Serveur principal"),
                view=None,
            )

        view = ConfirmView(interaction.user.id, confirmed, command="/setup serveur-principal")
        view.original = interaction
        await interaction.response.send_message(content=text, view=view, ephemeral=True)

    # --- Arrivée et départ du bot -----------------------------------------------------------

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        log.info("Bot invité sur le serveur %s (%s) : /setup etat y montre ce qui est prêt", guild.name, guild.id)

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild) -> None:
        log.info("Bot retiré du serveur %s (%s)", guild.name, guild.id)


async def setup(bot: SaeBot) -> None:
    await bot.add_cog(SetupCog(bot))
