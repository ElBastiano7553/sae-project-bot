"""Vues interactives (boutons) utilisées par les commandes."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

import discord

log = logging.getLogger(__name__)

ConfirmCallback = Callable[[discord.Interaction], Awaitable[None]]


class ConfirmView(discord.ui.View):
    """Boutons « Confirmer / Annuler » réservés à l'auteur de la commande, avec expiration."""

    def __init__(self, author_id: int, on_confirm: ConfirmCallback, *, timeout: float = 120) -> None:
        super().__init__(timeout=timeout)
        self._author_id = author_id
        self._on_confirm = on_confirm
        self.original: discord.Interaction | None = None  # renseigné après l'envoi
        self.done = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self._author_id:
            await interaction.response.send_message(
                "Seule la personne qui a lancé la commande peut confirmer.", ephemeral=True
            )
            return False
        return not self.done

    @discord.ui.button(label="Confirmer", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.done = True
        self.stop()
        await interaction.response.edit_message(content="⏳ Application en cours…", view=None)
        await self._on_confirm(interaction)

    @discord.ui.button(label="Annuler", style=discord.ButtonStyle.secondary, emoji="✖️")
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.done = True
        self.stop()
        await interaction.response.edit_message(content="Opération annulée : rien n'a été modifié.", view=None)

    async def on_timeout(self) -> None:
        if self.done or self.original is None:
            return
        try:
            await self.original.edit_original_response(
                content="⌛ Délai de confirmation expiré : rien n'a été modifié. Relancez /setup.", view=None
            )
        except discord.HTTPException:
            log.debug("Impossible de signaler l'expiration de la confirmation", exc_info=True)
