"""Assistant de projet (1.14.0) : ``/samae demande:…`` et, si ASSISTANT_ON_MENTION=true, « @Samae … ».

Consultations automatiques ; toute modification passe par un bouton et un formulaire que la
personne valide elle-même. Réponses éphémères à la commande ; réponse dans le salon à une mention
(sans aucune mention qui notifie). Les questions libres n'utilisent l'IA que si elle est configurée.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from src.ai import AIError
from src.discord_bot.bot import SaeBot, require_team_space, send_error
from src.discord_bot.embeds import add_field, make_embed
from src.discord_bot.permissions import is_bot_admin
from src.services.ai_assistant import QuotaExceeded
from src.services.meetings import Actor, RateLimiter
from src.services.project_assistant import AssistantReply

log = logging.getLogger(__name__)
NO_MENTIONS = discord.AllowedMentions.none()


def reply_embed(reply: AssistantReply, question: str) -> discord.Embed:
    embed = make_embed(reply.title, "\n".join(reply.lines) or "—", footer=f"Demande : {question[:150]} · Samae")
    if reply.sources:
        add_field(embed, "Sources", "\n".join(reply.sources[:8]))
    if reply.notes:
        add_field(embed, "À savoir", "\n".join(reply.notes))
    if reply.used_ai:
        add_field(embed, "IA", "Réponse rédigée par l'IA à partir des données ci-dessus : vérifiez avant de décider.")
    return embed


class AddIdeaView(discord.ui.View):
    """Bouton « Compléter et ajouter » : ouvre le formulaire d'idée prérempli (rien n'est enregistré avant)."""

    def __init__(self, cog, author_id: int, name: str, description: str) -> None:
        super().__init__(timeout=600)
        self.cog = cog
        self.author_id = author_id
        self.name = name
        self.description = description

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Seule la personne qui a fait la demande peut ajouter cette idée.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Compléter et ajouter", style=discord.ButtonStyle.success)
    async def add(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        ideas_cog = self.cog.bot.get_cog("IdeasCog")
        if ideas_cog is None:
            await interaction.response.send_message("Le catalogue d'idées est indisponible.", ephemeral=True)
            return
        await interaction.response.send_modal(ideas_cog.idea_modal(name=self.name, description=self.description))


class AssistantCog(commands.Cog):
    def __init__(self, bot: SaeBot) -> None:
        self.bot = bot
        # Mentions : au plus 6 par minute et par membre (les commandes ont déjà le contrôle de Discord).
        self._mention_limiter = RateLimiter(6, 60)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return require_team_space(interaction)

    def _actor(self, user, guild_id: int | None) -> Actor:
        admin = isinstance(user, discord.Member) and is_bot_admin(user, self.bot.settings.admin_role_name)
        return Actor(user.id, (getattr(user, "display_name", None) or str(user))[:100], admin, guild_id)

    async def _answer(self, question: str, actor: Actor) -> AssistantReply:
        return await self.bot.project_assistant.answer(question, actor)

    @app_commands.command(name="samae", description="Demander à Samae : bilan, fonctionnalités restantes, décisions, réunion…")
    @app_commands.describe(demande="Par exemple : « Fais-moi le bilan de notre avancement »")
    @app_commands.guild_only()
    async def samae(self, interaction: discord.Interaction, demande: app_commands.Range[str, 3, 500]) -> None:
        actor = self._actor(interaction.user, interaction.guild_id)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            reply = await self._answer(demande, actor)
        except (QuotaExceeded, AIError) as exc:
            await send_error(interaction, str(exc), title="L'IA n'a pas pu répondre", tone="warning")
            return
        view = None
        if reply.action and reply.action.kind == "add_idea":
            view = AddIdeaView(self, actor.user_id, reply.action.name, reply.action.description)
        kwargs = {"embed": reply_embed(reply, demande), "ephemeral": True}
        if view is not None:
            kwargs["view"] = view
        await interaction.followup.send(**kwargs)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """« @Samae quelles idées sont en attente ? » (ASSISTANT_ON_MENTION=true, serveur principal seulement)."""
        if not self.bot.settings.assistant_on_mention or message.author.bot or message.guild is None:
            return
        me = self.bot.user
        if me is None or me not in message.mentions:
            return
        if message.guild.id != self.bot.home_guild_id:
            return
        question = message.content.replace(f"<@{me.id}>", "").replace(f"<@!{me.id}>", "").strip()
        if len(question) < 3:
            return
        if self._mention_limiter.hit(message.author.id) is not None:
            return
        actor = self._actor(message.author, message.guild.id)
        try:
            reply = await self._answer(question, actor)
        except (QuotaExceeded, AIError) as exc:
            await message.reply(str(exc), allowed_mentions=NO_MENTIONS)
            return
        if reply.action is not None:
            reply.lines.append("Pour l'ajouter, utilisez `/samae` avec la même demande (validation par formulaire).")
        try:
            await message.reply(embed=reply_embed(reply, question), allowed_mentions=NO_MENTIONS)
        except discord.HTTPException:
            log.warning("Réponse de l'assistant impossible à publier", exc_info=True)


async def setup(bot: SaeBot) -> None:
    await bot.add_cog(AssistantCog(bot))
