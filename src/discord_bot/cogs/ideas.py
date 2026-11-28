"""Gestionnaire de réunions (1.14.0) : /idee, /tierlist et /debat.

Comme le cog des réunions, ce cog ne décide rien : il identifie la personne (et si elle est
administratrice du bot), appelle ``services.ideas`` qui applique les règles, puis répond de façon
éphémère. Seules les publications explicites (résultats, décision confirmée) sont visibles de tous,
sans aucune mention qui notifie.
"""

from __future__ import annotations

import logging
import re

import discord
from discord import app_commands
from discord.ext import commands

from src.discord_bot.bot import SaeBot, send_error
from src.discord_bot.cogs.admin import admin_only
from src.discord_bot.embeds import make_embed, success_embed
from src.discord_bot.ideas_ui import (
    BallotView,
    IdeaModal,
    OptionVoteView,
    catalogue_pages,
    debate_embed,
    idea_embed,
    idea_line,
    results_embeds,
    tierlist_embed,
)
from src.discord_bot.meeting_ui import PagesView
from src.discord_bot.permissions import is_bot_admin
from src.discord_bot.views import ConfirmView
from src.services import text_limits as limits
from src.services.design import escape_markdown as _md
from src.services.ideas import (
    ACTIVE,
    DEBATE_STATUS_LABELS,
    IDEA_STATUS_LABELS,
    REJECTED,
    RETAINED,
    WITHDRAWN,
    IdeaError,
)
from src.services.meetings import Actor, MeetingError

log = logging.getLogger(__name__)
NO_MENTIONS = discord.AllowedMentions.none()

TIER_CHOICES = [app_commands.Choice(name=f"{t} ({s})", value=t) for t, s in zip("SABCDF", (6, 5, 4, 3, 2, 1), strict=True)]
STATUS_CHOICES = [
    app_commands.Choice(name="Retenue", value=RETAINED),
    app_commands.Choice(name="Écartée", value=REJECTED),
    app_commands.Choice(name="De nouveau en attente", value=ACTIVE),
]
CATALOGUE_CHOICES = [
    app_commands.Choice(name="En attente et retenues", value="courantes"),
    app_commands.Choice(name="Retenues", value=RETAINED),
    app_commands.Choice(name="Écartées", value=REJECTED),
    app_commands.Choice(name="Retirées", value=WITHDRAWN),
    app_commands.Choice(name="Toutes", value="toutes"),
]
CATALOGUE_STATUSES = {"courantes": (ACTIVE, RETAINED), RETAINED: (RETAINED,), REJECTED: (REJECTED,),
                      WITHDRAWN: (WITHDRAWN,), "toutes": (ACTIVE, RETAINED, REJECTED, WITHDRAWN)}
ENTRY_CHOICES = [
    app_commands.Choice(name="Argument pour", value="pour"),
    app_commands.Choice(name="Argument contre", value="contre"),
    app_commands.Choice(name="Objection", value="objection"),
]
PROPOSAL_STATUS_CHOICES = [
    app_commands.Choice(name="Retenir l'idée", value=RETAINED),
    app_commands.Choice(name="Écarter l'idée", value=REJECTED),
]


def _bot(interaction: discord.Interaction) -> SaeBot:
    return interaction.client  # type: ignore[return-value]


async def idea_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[int]]:
    bot = _bot(interaction)
    ideas = bot.ideas_repo.list(interaction.guild_id, statuses=(ACTIVE, RETAINED, REJECTED), limit=500)
    text = current.strip().casefold()
    matches = [i for i in reversed(ideas) if not text or text in i.name.casefold() or text == str(i.id)]
    return [app_commands.Choice(name=limits.truncate(f"#{i.id} {i.name} ({IDEA_STATUS_LABELS[i.status]})", 100), value=i.id)
            for i in matches[:25]]


async def tierlist_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[int]]:
    bot = _bot(interaction)
    text = current.strip().casefold()
    lists = [t for t in bot.tierlists_repo.list(interaction.guild_id, limit=50) if not text or text in t.title.casefold()]
    states = {"open": "ouverte", "closed": "clôturée", "cancelled": "annulée"}
    return [app_commands.Choice(name=limits.truncate(f"#{t.id} {t.title} ({states[t.status]})", 100), value=t.id) for t in lists[:25]]


async def debate_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[int]]:
    bot = _bot(interaction)
    text = current.strip().casefold()
    debates = [d for d in bot.debates_repo.list(interaction.guild_id, limit=50) if not text or text in d.title.casefold()]
    return [app_commands.Choice(name=limits.truncate(f"#{d.id} {d.title} ({DEBATE_STATUS_LABELS[d.status]})", 100), value=d.id)
            for d in debates[:25]]


def parse_ids(text: str | None) -> list[int]:
    return [int(x) for x in re.findall(r"\d+", text or "")]


class IdeasCog(commands.Cog):
    idee = app_commands.Group(name="idee", description="Idées de jeu : proposer, consulter, regrouper (5 idées en attente par membre)",
                              guild_only=True)
    tierlist = app_commands.Group(name="tierlist", description="Tier lists S à F à votes masqués, résultats et désaccords",
                                  guild_only=True)
    debat = app_commands.Group(name="debat", description="Débats : arguments, objections, départage et décision confirmée",
                               guild_only=True)

    def __init__(self, bot: SaeBot) -> None:
        self.bot = bot

    def _actor(self, interaction: discord.Interaction) -> Actor:
        user = interaction.user
        admin = isinstance(user, discord.Member) and is_bot_admin(user, self.bot.settings.admin_role_name)
        name = getattr(user, "display_name", None) or str(user)
        return Actor(user.id, name[:100], admin, interaction.guild_id)

    async def _fail(self, interaction: discord.Interaction, exc: MeetingError) -> None:
        await send_error(interaction, str(exc), title=exc.title, tone="warning")

    async def _ok(self, interaction: discord.Interaction, text: str, *, embed: discord.Embed | None = None) -> None:
        embeds = [success_embed(text)] + ([embed] if embed else [])
        if interaction.response.is_done():
            await interaction.followup.send(embeds=embeds, ephemeral=True)
        else:
            await interaction.response.send_message(embeds=embeds, ephemeral=True)

    async def _pages(self, interaction: discord.Interaction, pages: list[discord.Embed]) -> None:
        if len(pages) == 1:
            await interaction.response.send_message(embed=pages[0], ephemeral=True)
            return
        view = PagesView(pages, interaction.user.id)
        await interaction.response.send_message(embed=pages[0], view=view, ephemeral=True)
        view.original = interaction

    # --- /idee -------------------------------------------------------------------------

    @idee.command(name="proposer", description="Proposer une idée de jeu (formulaire : nom, style, genre, description)")
    @app_commands.describe(reunion="Numéro de la réunion à laquelle rattacher l'idée (facultatif)")
    async def idea_propose(self, interaction: discord.Interaction, reunion: int | None = None) -> None:
        await interaction.response.send_modal(self.idea_modal(reunion=reunion))

    def idea_modal(self, *, reunion: int | None = None, name: str = "", description: str = "") -> IdeaModal:
        async def submit(modal_interaction: discord.Interaction, values: dict[str, str]) -> None:
            actor = self._actor(modal_interaction)
            try:
                if reunion is not None:
                    self.bot.meetings.get(reunion, actor.guild_id)
                idea, similar = self.bot.ideas.propose(actor, meeting_id=reunion, **values)
            except MeetingError as exc:
                await self._fail(modal_interaction, exc)
                return
            await self._ok(modal_interaction, f"Idée #{idea.id} ajoutée au catalogue.", embed=idea_embed(idea, similar))

        return IdeaModal(submit, name=name, description=description)

    @idee.command(name="catalogue", description="Catalogue collectif des idées, regroupées par famille et par genre")
    @app_commands.choices(statut=CATALOGUE_CHOICES)
    async def idea_catalogue(self, interaction: discord.Interaction, statut: app_commands.Choice[str] | None = None) -> None:
        key = statut.value if statut else "courantes"
        families = self.bot.ideas.catalogue(self._actor(interaction), statuses=CATALOGUE_STATUSES[key])
        await self._pages(interaction, catalogue_pages(families, f"Catalogue des idées · {statut.name if statut else 'en attente et retenues'}"))

    @idee.command(name="voir", description="Détail d'une idée, idées proches et historique")
    @app_commands.autocomplete(idee=idea_autocomplete)
    async def idea_show(self, interaction: discord.Interaction, idee: int) -> None:
        actor = self._actor(interaction)
        try:
            idea = self.bot.ideas.get(idee, actor)
        except MeetingError as exc:
            await self._fail(interaction, exc)
            return
        embed = idea_embed(idea, self.bot.ideas.similar_to(idea), self.bot.ideas_repo.history(idea.id))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @idee.command(name="chercher", description="Rechercher une idée (nom, style, genre, description)")
    async def idea_search(self, interaction: discord.Interaction, texte: app_commands.Range[str, 2, 100]) -> None:
        ideas = self.bot.ideas.search(self._actor(interaction), texte)
        lines = [idea_line(i) for i in ideas] or ["Aucune idée ne correspond."]
        await interaction.response.send_message(embed=make_embed(f"Recherche « {texte} »", "\n".join(lines)), ephemeral=True)

    @idee.command(name="mes-idees", description="Vos idées et le nombre d'idées en attente qu'il vous reste")
    async def idea_mine(self, interaction: discord.Interaction) -> None:
        actor = self._actor(interaction)
        ideas = self.bot.ideas_repo.list(actor.guild_id, author_id=actor.user_id)
        active = sum(1 for i in ideas if i.status == ACTIVE)
        lines = [idea_line(i, with_author=False) for i in ideas] or ["Aucune idée : `/idee proposer`."]
        embed = make_embed(f"Mes idées · {active}/5 en attente", "\n".join(lines))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @idee.command(name="modifier", description="Modifier votre idée tant qu'elle est en attente")
    @app_commands.autocomplete(idee=idea_autocomplete)
    async def idea_edit(
        self, interaction: discord.Interaction, idee: int, nom: str | None = None, style: str | None = None,
        genre: str | None = None, description: str | None = None,
    ) -> None:
        try:
            idea = self.bot.ideas.edit(idee, self._actor(interaction), name=nom, play_style=style, genre=genre, description=description)
        except MeetingError as exc:
            await self._fail(interaction, exc)
            return
        await self._ok(interaction, f"Idée #{idea.id} modifiée.", embed=idea_embed(idea))

    @idee.command(name="retirer", description="Retirer votre idée (elle reste dans l'historique)")
    @app_commands.autocomplete(idee=idea_autocomplete)
    async def idea_withdraw(self, interaction: discord.Interaction, idee: int) -> None:
        try:
            idea = self.bot.ideas.withdraw(idee, self._actor(interaction))
        except MeetingError as exc:
            await self._fail(interaction, exc)
            return
        await self._ok(interaction, f"Idée #{idea.id} retirée : elle reste visible dans `/idee catalogue statut:Retirées`.")

    @idee.command(name="regrouper", description="Regrouper deux idées proches dans la même famille")
    @app_commands.autocomplete(idee=idea_autocomplete, avec=idea_autocomplete)
    async def idea_group(self, interaction: discord.Interaction, idee: int, avec: int) -> None:
        try:
            self.bot.ideas.group(idee, avec, self._actor(interaction))
        except MeetingError as exc:
            await self._fail(interaction, exc)
            return
        await self._ok(interaction, f"Idée #{idee} regroupée avec l'idée #{avec}.")

    @idee.command(name="statut", description="Admins : déclarer une idée retenue, écartée ou de nouveau en attente",
                  extras={"admin": True})
    @app_commands.autocomplete(idee=idea_autocomplete)
    @app_commands.choices(statut=STATUS_CHOICES)
    @admin_only()
    async def idea_status(self, interaction: discord.Interaction, idee: int, statut: app_commands.Choice[str], motif: str | None = None) -> None:
        try:
            idea = self.bot.ideas.set_status(idee, self._actor(interaction), statut.value, motif or "")
        except MeetingError as exc:
            await self._fail(interaction, exc)
            return
        await self._ok(interaction, f"Idée #{idea.id} : {IDEA_STATUS_LABELS[idea.status]}.")

    # --- /tierlist ---------------------------------------------------------------------

    @tierlist.command(name="creer", description="Admins : créer une tier list (toutes les idées en attente, ou une liste)",
                      extras={"admin": True})
    @app_commands.describe(idees="Numéros des idées, séparés par des virgules (par défaut : toutes les idées en attente)",
                           reunion="Réunion à laquelle rattacher la tier list (compte rendu)")
    @admin_only()
    async def tl_create(self, interaction: discord.Interaction, titre: app_commands.Range[str, 1, 100],
                        idees: str | None = None, reunion: int | None = None) -> None:
        actor = self._actor(interaction)
        try:
            if reunion is not None:
                self.bot.meetings.get(reunion, actor.guild_id)
            tl = self.bot.tierlists.create(actor, titre, idea_ids=parse_ids(idees) or None, meeting_id=reunion)
        except MeetingError as exc:
            await self._fail(interaction, exc)
            return
        items = self.bot.tierlists.items(tl)
        embed = tierlist_embed(tl, items, self.bot.tierlists.progress(tl.id, actor))
        await interaction.response.send_message(
            content=f"Tier list #{tl.id} ouverte : chacun vote avec `/tierlist voter tierlist:{tl.id}`.",
            embed=embed, allowed_mentions=NO_MENTIONS,
        )

    @tierlist.command(name="voter", description="Ouvrir votre bulletin (un menu S à F par idée, votes masqués)")
    @app_commands.autocomplete(tierlist=tierlist_autocomplete)
    async def tl_vote(self, interaction: discord.Interaction, tierlist: int) -> None:
        actor = self._actor(interaction)
        try:
            tl = self.bot.tierlists.get(tierlist, actor)
            ballot, validated = self.bot.tierlists.my_ballot(tl.id, actor)
            if tl.status != "open":
                raise IdeaError("Cette tier list est clôturée : consultez `/tierlist resultats`.")
            if validated:
                raise IdeaError("Votre bulletin est déjà validé : il n'est plus modifiable.")
        except MeetingError as exc:
            await self._fail(interaction, exc)
            return
        view = BallotView(
            actor.user_id, tl.id, self.bot.tierlists.items(tl), ballot,
            on_vote=lambda idea_id, tier: self.bot.tierlists.vote(tl.id, actor, idea_id, tier),
            on_validate=lambda: self.bot.tierlists.validate(tl.id, actor),
        )
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)
        view.original = interaction

    @tierlist.command(name="classer", description="Classer une idée sans ouvrir le bulletin (modifiable avant validation)")
    @app_commands.autocomplete(tierlist=tierlist_autocomplete, idee=idea_autocomplete)
    @app_commands.choices(rang=TIER_CHOICES)
    async def tl_rank(self, interaction: discord.Interaction, tierlist: int, idee: int, rang: app_commands.Choice[str]) -> None:
        try:
            ballot = self.bot.tierlists.vote(tierlist, self._actor(interaction), idee, rang.value)
        except MeetingError as exc:
            await self._fail(interaction, exc)
            return
        total = len(self.bot.tierlists_repo.item_ids(tierlist))
        await self._ok(interaction, f"Idée #{idee} classée **{rang.value}** ({len(ballot)}/{total} idées classées). "
                       "Validez avec `/tierlist valider` quand tout est classé.")

    @tierlist.command(name="valider", description="Valider votre bulletin (il ne sera plus modifiable)")
    @app_commands.autocomplete(tierlist=tierlist_autocomplete)
    async def tl_validate(self, interaction: discord.Interaction, tierlist: int) -> None:
        try:
            self.bot.tierlists.validate(tierlist, self._actor(interaction))
        except MeetingError as exc:
            await self._fail(interaction, exc)
            return
        await self._ok(interaction, f"Bulletin de la tier list #{tierlist} validé. Merci !")

    @tierlist.command(name="progression", description="Qui a validé son bulletin (aucun vote n'est montré)")
    @app_commands.autocomplete(tierlist=tierlist_autocomplete)
    async def tl_progress(self, interaction: discord.Interaction, tierlist: int) -> None:
        actor = self._actor(interaction)
        try:
            tl = self.bot.tierlists.get(tierlist, actor)
        except MeetingError as exc:
            await self._fail(interaction, exc)
            return
        embed = tierlist_embed(tl, self.bot.tierlists.items(tl), self.bot.tierlists.progress(tl.id, actor))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @tierlist.command(name="cloturer", description="Admins : clôturer la tier list et publier les résultats agrégés",
                      extras={"admin": True})
    @app_commands.autocomplete(tierlist=tierlist_autocomplete)
    @admin_only()
    async def tl_close(self, interaction: discord.Interaction, tierlist: int) -> None:
        actor = self._actor(interaction)
        try:
            tl = self.bot.tierlists.get(tierlist, actor)
            progress = self.bot.tierlists.progress(tl.id, actor)
        except MeetingError as exc:
            await self._fail(interaction, exc)
            return

        async def confirmed(confirm_interaction: discord.Interaction) -> None:
            try:
                closed = self.bot.tierlists.close(tl.id, actor)
            except MeetingError as exc:
                await confirm_interaction.edit_original_response(content=str(exc), embeds=[], view=None)
                return
            await confirm_interaction.edit_original_response(content="Tier list clôturée.", embeds=[], view=None)
            channel = confirm_interaction.channel
            for embed in results_embeds(closed, closed.results_data or {"ideas": []}):
                await channel.send(embed=embed, allowed_mentions=NO_MENTIONS)

        warning = f" {len(progress['started'])} bulletin(s) non validé(s) ne seront pas comptés." if progress["started"] else ""
        view = ConfirmView(interaction.user.id, confirmed, command="/tierlist cloturer")
        await interaction.response.send_message(
            f"Clôturer la tier list #{tl.id} ? {len(progress['validated'])} bulletin(s) validé(s).{warning} "
            "Les résultats agrégés (sans aucun vote individuel) seront publiés dans ce salon.",
            view=view, ephemeral=True,
        )
        view.original = interaction

    @tierlist.command(name="resultats", description="Résultats d'une tier list clôturée : moyennes, égalités, désaccords")
    @app_commands.autocomplete(tierlist=tierlist_autocomplete)
    async def tl_results(self, interaction: discord.Interaction, tierlist: int) -> None:
        actor = self._actor(interaction)
        try:
            data = self.bot.tierlists.results(tierlist, actor)
            tl = self.bot.tierlists.get(tierlist, actor)
        except MeetingError as exc:
            await self._fail(interaction, exc)
            return
        await self._pages(interaction, results_embeds(tl, data))

    @tierlist.command(name="liste", description="Tier lists ouvertes et clôturées")
    async def tl_list(self, interaction: discord.Interaction) -> None:
        lists = self.bot.tierlists_repo.list(interaction.guild_id, limit=20)
        states = {"open": "votes en cours", "closed": "clôturée", "cancelled": "annulée"}
        lines = [f"#{t.id} **{_md(t.title)}** · {states[t.status]} · {t.created_at[:10]}" for t in lists] or [
            "Aucune tier list : un administrateur en crée une avec `/tierlist creer`."]
        await interaction.response.send_message(embed=make_embed("Tier lists", "\n".join(lines)), ephemeral=True)

    # --- /debat ------------------------------------------------------------------------

    @debat.command(name="controverses", description="Idées controversées ou à égalité d'une tier list clôturée")
    @app_commands.autocomplete(tierlist=tierlist_autocomplete)
    async def debate_controversial(self, interaction: discord.Interaction, tierlist: int) -> None:
        try:
            rows = self.bot.debates.controversial(tierlist, self._actor(interaction))
        except MeetingError as exc:
            await self._fail(interaction, exc)
            return
        lines = [
            f"#{r['idea_id']} **{_md(r['name'])}** · {r['tier']} ({r['mean']:.2f}) · écart type {r['stdev']:.2f}"
            + (" · désaccord" if r.get("disagreement") else "") + (f" · {len(r['extremes'])} avis extrême(s)" if r.get("extremes") else "")
            for r in rows if r.get("mean") is not None
        ] or ["Aucune idée controversée : le classement fait consensus."]
        lines.append(f"\nOuvrir un débat : `/debat ouvrir idee:<numéro> tierlist:{tierlist}`")
        await interaction.response.send_message(embed=make_embed(f"À débattre · tier list #{tierlist}", "\n".join(lines)), ephemeral=True)

    @debat.command(name="ouvrir", description="Ouvrir un débat sur une idée ou sur un sujet libre")
    @app_commands.autocomplete(idee=idea_autocomplete, tierlist=tierlist_autocomplete)
    async def debate_open(self, interaction: discord.Interaction, sujet: app_commands.Range[str, 1, 150] | None = None,
                          idee: int | None = None, tierlist: int | None = None, reunion: int | None = None) -> None:
        actor = self._actor(interaction)
        try:
            if sujet is None and idee is None:
                raise IdeaError("Indiquez un sujet ou une idée.")
            if reunion is not None:
                self.bot.meetings.get(reunion, actor.guild_id)
            debate = self.bot.debates.open(actor, title=sujet, idea_id=idee, tierlist_id=tierlist, meeting_id=reunion)
        except MeetingError as exc:
            await self._fail(interaction, exc)
            return
        await interaction.response.send_message(
            content="Débat ouvert : ajoutez vos arguments avec `/debat argument`.",
            embed=self._debate_embed(debate), allowed_mentions=NO_MENTIONS,
        )

    def _debate_embed(self, debate) -> discord.Embed:
        idea = self.bot.ideas_repo.get(debate.idea_id) if debate.idea_id else None
        tally = self.bot.debates.tally(debate) if debate.status != "open" else None
        return debate_embed(debate, self.bot.debates_repo.entries(debate.id), tally, idea)

    @debat.command(name="voir", description="Arguments, objections, départage et décision d'un débat")
    @app_commands.autocomplete(debat=debate_autocomplete)
    async def debate_show(self, interaction: discord.Interaction, debat: int) -> None:
        try:
            debate = self.bot.debates.get(debat, self._actor(interaction))
        except MeetingError as exc:
            await self._fail(interaction, exc)
            return
        await interaction.response.send_message(embed=self._debate_embed(debate), ephemeral=True)

    @debat.command(name="liste", description="Débats ouverts, en vote et décidés")
    async def debate_list(self, interaction: discord.Interaction) -> None:
        debates = self.bot.debates_repo.list(interaction.guild_id, limit=20)
        lines = [f"#{d.id} **{_md(d.title)}** · {DEBATE_STATUS_LABELS[d.status]}" for d in debates] or ["Aucun débat."]
        await interaction.response.send_message(embed=make_embed("Débats", "\n".join(lines)), ephemeral=True)

    @debat.command(name="argument", description="Ajouter un argument pour, contre, ou une objection")
    @app_commands.autocomplete(debat=debate_autocomplete)
    @app_commands.choices(type=ENTRY_CHOICES)
    async def debate_argument(self, interaction: discord.Interaction, debat: int, type: app_commands.Choice[str],
                              texte: app_commands.Range[str, 1, 500]) -> None:
        try:
            self.bot.debates.add_entry(debat, self._actor(interaction), type.value, texte)
        except MeetingError as exc:
            await self._fail(interaction, exc)
            return
        await self._ok(interaction, f"{type.name} ajouté au débat #{debat}.")

    @debat.command(name="departage", description="Admins : lancer un vote de départage (2 à 5 options séparées par « ; »)",
                   extras={"admin": True})
    @app_commands.autocomplete(debat=debate_autocomplete)
    @admin_only()
    async def debate_tiebreak(self, interaction: discord.Interaction, debat: int, options: app_commands.Range[str, 3, 450]) -> None:
        try:
            debate = self.bot.debates.start_vote(debat, self._actor(interaction), re.split(r"[;|]", options))
        except MeetingError as exc:
            await self._fail(interaction, exc)
            return
        await interaction.response.send_message(
            content=f"Vote de départage ouvert : votez avec `/debat voter debat:{debate.id}` (un vote par membre, modifiable).",
            embed=self._debate_embed(debate), allowed_mentions=NO_MENTIONS,
        )

    @debat.command(name="voter", description="Voter au départage d'un débat (modifiable tant que le vote est ouvert)")
    @app_commands.autocomplete(debat=debate_autocomplete)
    async def debate_vote(self, interaction: discord.Interaction, debat: int) -> None:
        actor = self._actor(interaction)
        try:
            debate = self.bot.debates.get(debat, actor)
            if debate.status != "voting":
                raise IdeaError("Aucun vote de départage en cours pour ce débat.")
        except MeetingError as exc:
            await self._fail(interaction, exc)
            return
        options = self.bot.debates_repo.options(debate.id)
        view = OptionVoteView(actor.user_id, options, self.bot.debates_repo.my_vote(debate.id, actor.user_id),
                              lambda option_id: self.bot.debates.vote(debate.id, actor, option_id))
        await interaction.response.send_message(f"Départage du débat #{debate.id} : {debate.title}", view=view, ephemeral=True)

    @debat.command(name="proposer", description="Proposer la décision d'un débat (un administrateur devra la confirmer)")
    @app_commands.autocomplete(debat=debate_autocomplete)
    @app_commands.choices(idee_statut=PROPOSAL_STATUS_CHOICES)
    async def debate_propose(self, interaction: discord.Interaction, debat: int, decision: app_commands.Range[str, 1, 500],
                             idee_statut: app_commands.Choice[str] | None = None) -> None:
        try:
            debate = self.bot.debates.propose(debat, self._actor(interaction), decision, idee_statut.value if idee_statut else None)
        except MeetingError as exc:
            await self._fail(interaction, exc)
            return
        await self._ok(interaction, f"Décision proposée pour le débat #{debate.id} : un administrateur doit la confirmer "
                       f"avec `/debat confirmer debat:{debate.id}`.", embed=self._debate_embed(debate))

    @debat.command(name="confirmer", description="Admins : confirmer la décision proposée (elle devient officielle)",
                   extras={"admin": True})
    @app_commands.autocomplete(debat=debate_autocomplete)
    @admin_only()
    async def debate_confirm(self, interaction: discord.Interaction, debat: int) -> None:
        actor = self._actor(interaction)
        try:
            debate = self.bot.debates.get(debat, actor)
            if debate.status != "proposed" or not debate.proposal:
                raise IdeaError("Aucune décision proposée pour ce débat : `/debat proposer` d'abord.")
        except MeetingError as exc:
            await self._fail(interaction, exc)
            return
        proposal = debate.proposal

        async def confirmed(confirm_interaction: discord.Interaction) -> None:
            try:
                decided = self.bot.debates.confirm(debate.id, actor, proposal)
            except MeetingError as exc:
                await confirm_interaction.edit_original_response(content=str(exc), embeds=[], view=None)
                return
            await confirm_interaction.edit_original_response(content="Décision confirmée.", embeds=[], view=None)
            await confirm_interaction.channel.send(
                content=f"Décision officielle du débat #{decided.id}.", embed=self._debate_embed(decided), allowed_mentions=NO_MENTIONS
            )

        view = ConfirmView(interaction.user.id, confirmed, command="/debat confirmer")
        status = {RETAINED: " L'idée sera déclarée retenue.", REJECTED: " L'idée sera déclarée écartée."}.get(debate.proposal_idea_status or "", "")
        await interaction.response.send_message(
            content=f"Confirmer cette décision officielle ?{status} Elle sera aussi ajoutée aux DECISIONS du suivi si le classeur est importé.",
            embed=self._debate_embed(debate), view=view, ephemeral=True,
        )
        view.original = interaction

    @debat.command(name="annuler", description="Annuler un débat (auteur ou administrateur) ; son contenu est conservé")
    @app_commands.autocomplete(debat=debate_autocomplete)
    async def debate_cancel(self, interaction: discord.Interaction, debat: int) -> None:
        try:
            self.bot.debates.cancel(debat, self._actor(interaction))
        except MeetingError as exc:
            await self._fail(interaction, exc)
            return
        await self._ok(interaction, f"Débat #{debat} annulé (arguments conservés).")


async def setup(bot: SaeBot) -> None:
    await bot.add_cog(IdeasCog(bot))
