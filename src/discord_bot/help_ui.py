"""Centre d'aide interactif (/aide et /reunion aide) : pages, navigation et menu.

Le contenu vient de ``services.help`` (texte pédagogique) et de l'arbre des
commandes réellement enregistrées (noms, descriptions, paramètres) : la page
« Commandes » ne peut donc pas lister une commande qui n'existe plus.

Les boutons ne font que changer de page : aucun ne déclenche d'action
d'administration. La vue est réservée à la personne qui a ouvert l'aide.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import discord
from discord import app_commands

from src.discord_bot.embeds import add_field, fit_embed, make_embed
from src.services import design
from src.services import text_limits as limits
from src.services.help import CommandInfo, HelpPage, ParamInfo, command_lines, visible_sections

log = logging.getLogger(__name__)

HELP_TIMEOUT = 600
# Marge sous la limite de 6000 caractères d'un embed (titres et pied de page compris).
PAGE_BUDGET = 4500
SELECT_LIMIT = 25


def collect_commands(tree: app_commands.CommandTree) -> list[CommandInfo]:
    """Commandes enregistrées, avec leurs paramètres, lues directement dans discord.py."""
    result: list[CommandInfo] = []

    def visit(command, admin: bool) -> None:
        admin = admin or _is_admin(command)
        if isinstance(command, app_commands.Group):  # groupe ou sous-groupe (« reunion ajouter »)
            for sub in command.commands:
                visit(sub, admin)
        elif isinstance(command, app_commands.Command):
            result.append(_info(command, admin))

    for command in tree.get_commands():
        visit(command, False)
    return result


def _is_admin(command) -> bool:
    perms = getattr(command, "default_permissions", None)
    return bool(perms and perms.manage_guild)


def _info(command: app_commands.Command, admin: bool) -> CommandInfo:
    params = tuple(ParamInfo(p.name, p.description, p.required) for p in command.parameters)
    return CommandInfo(command.qualified_name, command.description, params, admin)


@dataclass(frozen=True)
class RenderedPage:
    key: str  # section de rattachement (pour le menu)
    title: str
    embed: discord.Embed


def render_pages(
    pages: tuple[HelpPage, ...] | list[HelpPage],
    *,
    commands: list[CommandInfo],
    admin: bool,
    heading: str,
) -> list[RenderedPage]:
    """Une ou plusieurs pages Discord par page de guide, toujours dans les limites de Discord."""
    blocks: list[tuple[HelpPage, str, list[tuple[str, str]]]] = []
    for page in pages:
        if page.generated == "commands":
            fields = []
            for category, items in command_lines(commands, include_admin=admin):
                for index, chunk in enumerate(limits.chunk_lines(items, limits.FIELD_VALUE)):
                    fields.append((category + (" (suite)" if index else ""), chunk))
            intro = (
                "Liste générée à partir des commandes enregistrées dans Discord : elle est toujours à jour. "
                "Dans Discord, tapez `/` puis le nom de la commande ; les options s'ajoutent avec Tab."
            )
            blocks.append((page, intro, fields))
        else:
            sections = [(s.title, s.body) for s in visible_sections(page, admin=admin)]
            blocks.append((page, page.intro, sections))

    rendered: list[tuple[HelpPage, str, list[tuple[str, str]], int]] = []
    for page, intro, fields in blocks:
        parts = _split(intro, fields)
        for index, (text, chunk) in enumerate(parts):
            rendered.append((page, text, chunk, index if len(parts) > 1 else -1))

    result = []
    total = len(rendered)
    for number, (page, intro, fields, part) in enumerate(rendered, start=1):
        title = f"{heading} · {page.title}" + (f" ({part + 1})" if part >= 0 else "")
        embed = make_embed(title, intro or None, footer=design.footer(heading.lower(), f"page {number}/{total}"))
        for name, value in fields:
            add_field(embed, name, value)
        result.append(RenderedPage(page.key, page.title, fit_embed(embed)))
    return result


def _split(intro: str, fields: list[tuple[str, str]]) -> list[tuple[str, list[tuple[str, str]]]]:
    """Répartit les champs d'une page en plusieurs embeds si elle est trop longue."""
    parts: list[tuple[str, list[tuple[str, str]]]] = []
    current: list[tuple[str, str]] = []
    size = len(intro)
    for name, value in fields:
        length = len(name) + min(len(value), limits.FIELD_VALUE)
        if current and (size + length > PAGE_BUDGET or len(current) >= limits.EMBED_FIELDS):
            parts.append((intro if not parts else "", current))
            current, size = [], 0
        current.append((name, value))
        size += length
    parts.append((intro if not parts else "", current))
    return parts


class HelpView(discord.ui.View):
    """Navigation : Précédent, Accueil, Suivant, indicateur de page et menu des sections."""

    def __init__(
        self,
        pages: list[RenderedPage],
        author_id: int,
        *,
        start: int = 0,
        home_label: str = "Accueil",
        timeout: float = HELP_TIMEOUT,
    ) -> None:
        super().__init__(timeout=timeout)
        self.pages = pages
        self.author_id = author_id
        self.home_label = home_label
        self.index = max(0, min(start, len(pages) - 1))
        self.original: discord.Interaction | None = None
        self._render()

    @property
    def embed(self) -> discord.Embed:
        return self.pages[self.index].embed

    def _render(self) -> None:
        self.clear_items()
        sections: list[RenderedPage] = []
        for page in self.pages:
            if all(page.key != s.key for s in sections):
                sections.append(page)
        current_key = self.pages[self.index].key
        if len(sections) > 1:
            select = discord.ui.Select(
                placeholder="Aller à une section",
                options=[
                    discord.SelectOption(label=limits.truncate(s.title, 100), value=s.key, default=s.key == current_key)
                    for s in sections[:SELECT_LIMIT]
                ],
                row=0,
            )
            select.callback = self._on_select
            self.add_item(select)
        self._button("Précédent", self._previous, disabled=self.index == 0)
        self._button(self.home_label, self._home, disabled=self.index == 0)
        self._button(f"{self.index + 1}/{len(self.pages)}", None, disabled=True)
        self._button("Suivant", self._next, disabled=self.index >= len(self.pages) - 1)

    def _button(self, label: str, callback, *, disabled: bool) -> None:
        button = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary, disabled=disabled, row=1)
        if callback is not None:
            button.callback = callback
        self.add_item(button)

    def go_to(self, index: int) -> None:
        self.index = max(0, min(index, len(self.pages) - 1))
        self._render()

    def go_to_section(self, key: str) -> None:
        self.go_to(next((i for i, page in enumerate(self.pages) if page.key == key), 0))

    async def _update(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(embed=self.embed, view=self)

    async def _previous(self, interaction: discord.Interaction) -> None:
        self.go_to(self.index - 1)
        await self._update(interaction)

    async def _next(self, interaction: discord.Interaction) -> None:
        self.go_to(self.index + 1)
        await self._update(interaction)

    async def _home(self, interaction: discord.Interaction) -> None:
        self.go_to(0)
        await self._update(interaction)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        values = (interaction.data or {}).get("values") or []
        if values:
            self.go_to_section(values[0])
        await self._update(interaction)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Cette aide a été ouverte par quelqu'un d'autre : tapez `/aide`.", ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        if self.original is None:
            return
        try:
            # La page reste lisible ; seuls les boutons disparaissent.
            await self.original.edit_original_response(view=None)
        except discord.HTTPException:
            log.debug("Impossible de retirer les boutons de l'aide expirée", exc_info=True)
