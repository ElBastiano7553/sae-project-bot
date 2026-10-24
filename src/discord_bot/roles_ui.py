"""Choix des rôles : bouton persistant « Choisir mes rôles » et menu de sélection privé.

Le bouton (identifiant fixe ``samae:roles:open``) se trouve sur le panneau des rôles,
le message de bienvenue et le message d'accueil d'un nouveau membre. Il ouvre, pour la
seule personne qui clique, un menu à choix multiples déjà coché avec ses rôles actuels.
Valider applique la différence : seuls les rôles proposés (``services.self_roles``)
peuvent être ajoutés ou retirés, et le catalogue est relu au moment de valider.
"""

from __future__ import annotations

import logging
import re

import discord

from src.discord_bot.embeds import error_embed, make_embed
from src.services import text_limits as limits
from src.services.design import Tone
from src.services.self_roles import ROLE_PICKER_ID, SelfRoleCatalog

log = logging.getLogger(__name__)

PICKER_TIMEOUT = 600


def picker_embed(catalog: SelfRoleCatalog, current: set[int]) -> discord.Embed:
    """Rôles proposés, cochés (✅) pour ceux que la personne a déjà."""
    embed = make_embed(
        "Choisir mes rôles",
        "Cochez dans le menu les domaines où vous travaillez et les notifications que vous voulez recevoir, "
        "puis fermez le menu : vos rôles sont mis à jour aussitôt. Décochez un rôle pour le retirer. "
        "Vous pouvez revenir ici quand vous voulez (`/roles`).",
    )
    for title, roles in catalog.grouped():
        lines = [f"{'✅' if role.role_id in current else '▫️'} **{role.name}** — {role.description}" for role in roles]
        for index, chunk in enumerate(limits.chunk_lines(lines, limits.FIELD_VALUE)):
            embed.add_field(name=title + (" (suite)" if index else ""), value=chunk, inline=False)
    embed.set_footer(text="Les rôles de responsabilité et d'administration sont attribués par un administrateur.")
    return embed


class RolePickerView(discord.ui.View):
    """Menu privé d'une personne : un seul menu à choix multiples, déjà coché avec ses rôles actuels."""

    def __init__(self, cog, catalog: SelfRoleCatalog, member_id: int, current: set[int], *, timeout: float = PICKER_TIMEOUT) -> None:
        super().__init__(timeout=timeout)
        self.cog = cog
        self.member_id = member_id
        options = [
            discord.SelectOption(
                label=limits.truncate(role.name, 100),
                value=str(role.role_id),
                description=limits.truncate(role.description, 100),
                default=role.role_id in current,
            )
            for role in catalog.roles
        ]
        select = discord.ui.Select(
            placeholder="Choisissez vos rôles (plusieurs choix possibles)",
            min_values=0,
            max_values=len(options),
            options=options,
        )
        select.callback = self._on_select
        self.select = select
        self.add_item(select)
        self.original: discord.Interaction | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.member_id:
            await interaction.response.send_message("Ce menu appartient à quelqu'un d'autre : tapez `/roles`.", ephemeral=True)
            return False
        return True

    async def _on_select(self, interaction: discord.Interaction) -> None:
        values = (interaction.data or {}).get("values") or []
        selected = [int(value) for value in values if str(value).isdigit()]
        await self.cog.apply_choice(interaction, selected, previous=self)

    async def on_timeout(self) -> None:
        if self.original is None:
            return
        try:
            await self.original.edit_original_response(view=None)
        except discord.HTTPException:
            log.debug("Impossible de retirer le menu des rôles expiré", exc_info=True)


class RolePickerButton(discord.ui.DynamicItem[discord.ui.Button], template=re.escape(ROLE_PICKER_ID)):
    """Bouton persistant : reconnu d'après son identifiant, même après un redémarrage du bot."""

    def __init__(self) -> None:
        super().__init__(discord.ui.Button(label="Choisir mes rôles", style=discord.ButtonStyle.primary, custom_id=ROLE_PICKER_ID))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /):
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("WelcomeCog")
        if cog is None:
            await interaction.response.send_message("Le choix des rôles est momentanément indisponible.", ephemeral=True)
            return
        try:
            await cog.open_picker(interaction)
        except Exception as exc:
            # Un clic doit toujours avoir une réponse : pas de « Échec de l'interaction » muet.
            log.error("Erreur sur le bouton du choix des rôles : %s", type(exc).__name__, exc_info=exc)
            await send_problem(interaction, "Le choix des rôles n'a pas abouti à cause d'une erreur inattendue. Réessayez ; si cela persiste, prévenez un administrateur.")


async def send_problem(interaction: discord.Interaction, text: str, *, title: str = "Choix des rôles impossible") -> None:
    embed = error_embed(text, title=title, tone="warning")
    try:
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)
    except discord.HTTPException:
        log.warning("Impossible d'afficher un message du choix des rôles", exc_info=True)


def result_embed(text: str, *, ok: bool) -> discord.Embed:
    return make_embed("Rôles mis à jour" if ok else "Rôles inchangés", text, tone=Tone.SUCCESS if ok else Tone.NEUTRAL)


