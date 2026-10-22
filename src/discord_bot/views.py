"""Vues interactives (boutons) utilisées par les commandes."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

import discord

from src.config import ConfigError

log = logging.getLogger(__name__)

ConfirmCallback = Callable[[discord.Interaction], Awaitable[None]]


class ConfirmView(discord.ui.View):
    """Boutons « Confirmer / Annuler » réservés à l'auteur de la commande, avec expiration."""

    def __init__(
        self,
        author_id: int,
        on_confirm: ConfirmCallback,
        *,
        timeout: float = 120,
        command: str = "/setup",
    ) -> None:
        super().__init__(timeout=timeout)
        self._author_id = author_id
        self._on_confirm = on_confirm
        self._command = command
        self.original: discord.Interaction | None = None  # renseigné après l'envoi
        self.done = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self._author_id:
            await interaction.response.send_message(
                "Seule la personne qui a lancé la commande peut confirmer.", ephemeral=True
            )
            return False
        if self.done:
            # Deuxième clic (ou interaction renvoyée par Discord) : l'action est déjà lancée, une seule fois.
            try:
                await interaction.response.defer()
            except discord.HTTPException:
                pass
            return False
        return True

    @discord.ui.button(label="Confirmer", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.done = True
        self.stop()
        await interaction.response.edit_message(content="Application en cours…", view=None)
        try:
            await self._on_confirm(interaction)
        except ConfigError as exc:
            await self._show_failure(interaction, f"**Problème de configuration** : {exc}")
        except Exception as exc:
            # Jamais de « Application en cours… » sans fin : l'échec est dit clairement.
            log.error("Erreur pendant la confirmation de %s : %s", self._command, type(exc).__name__, exc_info=exc)
            await self._show_failure(
                interaction,
                f"**Échec** : {self._command} s'est arrêté sur une erreur inattendue. "
                "Une partie des changements a pu être appliquée : vérifiez l'état avec `/status` "
                "(et `/admin messages` pour les messages permanents). Le détail est dans les journaux du bot.",
            )

    async def _show_failure(self, interaction: discord.Interaction, text: str) -> None:
        try:
            await interaction.edit_original_response(content=text, embeds=[], view=None)
        except discord.HTTPException:
            log.warning("Impossible d'afficher l'échec de la confirmation", exc_info=True)

    @discord.ui.button(label="Annuler", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.done = True
        self.stop()
        await interaction.response.edit_message(content="Opération annulée : rien n'a été modifié.", view=None)

    async def on_timeout(self) -> None:
        if self.done or self.original is None:
            return
        try:
            await self.original.edit_original_response(
                content=f"Délai de confirmation expiré : rien n'a été modifié. Relancez {self._command}.", view=None
            )
        except discord.HTTPException:
            log.debug("Impossible de signaler l'expiration de la confirmation", exc_info=True)


SubmitCallback = Callable[[discord.Interaction, str, str], Awaitable[None]]


class AnnouncementModal(discord.ui.Modal, title="Annonce en cours"):
    """Formulaire de /announce : le texte est prévisualisé avant toute publication."""

    def __init__(
        self,
        *,
        title_default: str,
        body_default: str,
        title_max: int,
        body_max: int,
        on_submit: SubmitCallback,
    ) -> None:
        super().__init__(timeout=600)
        self._on_submit = on_submit
        self.announcement_title = discord.ui.TextInput(
            label="Titre", default=title_default or None, max_length=title_max, required=True
        )
        self.body = discord.ui.TextInput(
            label="Texte de l'annonce",
            style=discord.TextStyle.paragraph,
            default=body_default or None,
            max_length=body_max,
            required=True,
        )
        self.add_item(self.announcement_title)
        self.add_item(self.body)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._on_submit(interaction, self.announcement_title.value, self.body.value)
