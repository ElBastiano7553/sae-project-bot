"""Boutons et menus de /projet configurer (connexion des projets GitHub, étape B4).

Chaque vue est réservée à la personne qui a lancé la commande ; l'action appelée revérifie
ensuite, côté bot, qu'elle est toujours administratrice de ce serveur.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

import discord

log = logging.getLogger(__name__)

Action = Callable[[discord.Interaction], Awaitable[None]]
RepositoryAction = Callable[[discord.Interaction, str], Awaitable[None]]
# Un menu Discord propose 25 choix au plus.
MAX_OPTIONS = 25


class _AuthorView(discord.ui.View):
    def __init__(self, author_id: int, *, timeout: float) -> None:
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.original: discord.Interaction | None = None  # renseigné après l'envoi

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Seule la personne qui a lancé la commande peut utiliser ces boutons.", ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        if self.original is None:
            return
        try:
            await self.original.edit_original_response(view=None)
        except discord.HTTPException:
            log.debug("Impossible de retirer les boutons expirés", exc_info=True)


class GithubConnectView(_AuthorView):
    """Lien « Installer sur GitHub » (jeton personnel, 15 minutes) et bouton « Choisir le dépôt »."""

    def __init__(self, author_id: int, install_url: str, on_choose: Action, *, timeout: float = 15 * 60) -> None:
        super().__init__(author_id, timeout=timeout)
        self._on_choose = on_choose
        self.add_item(discord.ui.Button(label="Installer sur GitHub", url=install_url, style=discord.ButtonStyle.link, row=0))

    @discord.ui.button(label="Choisir le dépôt", style=discord.ButtonStyle.primary, row=0)
    async def choose(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._on_choose(interaction)


class RepositorySelectView(_AuthorView):
    """Menu des dépôts des installations vérifiées pour ce serveur."""

    def __init__(self, author_id: int, repositories: list[str], on_select: RepositoryAction, *, timeout: float = 5 * 60) -> None:
        super().__init__(author_id, timeout=timeout)
        self._on_select = on_select
        select = discord.ui.Select(
            placeholder="Dépôt à connecter",
            options=[discord.SelectOption(label=name[:100], value=name[:100]) for name in repositories[:MAX_OPTIONS]],
        )
        select.callback = self._selected
        self.select = select
        self.add_item(select)

    async def _selected(self, interaction: discord.Interaction) -> None:
        self.stop()
        await self._on_select(interaction, self.select.values[0])


class TestMessageView(_AuthorView):
    """Bouton « Envoyer un message de test » après la connexion d'un projet."""

    __test__ = False  # nom commençant par « Test » : ce n'est pas une classe de tests pytest

    def __init__(self, author_id: int, on_test: Action, *, timeout: float = 10 * 60) -> None:
        super().__init__(author_id, timeout=timeout)
        self._on_test = on_test
        self.sent = False

    @discord.ui.button(label="Envoyer un message de test", style=discord.ButtonStyle.secondary)
    async def test(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.sent:  # un seul message de test par bouton
            await interaction.response.send_message("Le message de test a déjà été envoyé.", ephemeral=True)
            return
        self.sent = True
        await self._on_test(interaction)
