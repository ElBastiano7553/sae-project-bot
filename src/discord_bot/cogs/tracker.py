"""Suivi du projet (1.14.0) : groupe /suivi, le classeur « GAME DESIGN DATABASE » dans Discord.

Consultation libre ; ajout direct seulement pour les idées et les questions ouvertes ; tout autre
changement est une proposition qu'un administrateur valide. Import et export : administrateurs,
avec aperçu et confirmation. L'original du classeur n'est jamais modifié (voir docs/suivi-excel.md).
"""

from __future__ import annotations

import asyncio
import io
import logging

import discord
from discord import app_commands
from discord.ext import commands

from src.database.tracker import TrackerChange
from src.github.repo_sync import GitHubSyncError
from src.discord_bot.bot import SaeBot, require_team_space, send_error
from src.discord_bot.cogs.admin import admin_only
from src.discord_bot.embeds import add_field, make_embed, success_embed
from src.discord_bot.meeting_ui import PagesView
from src.discord_bot.permissions import is_bot_admin
from src.discord_bot.views import ConfirmView
from src.services import text_limits as limits
from src.services.design import escape_markdown as _md
from src.services.meetings import Actor, MeetingError
from src.services.repo_sync import PROGRESS_SHEETS, SyncResult
from src.services.tracker import DECISIONS, FEATURES, IDEAS, OPEN_QUESTIONS, RowView, TrackerError
from src.tracker.checks import Level, Report, finding_line, sheets_from_xlsx, verify_workbook
from src.tracker.checks import report_markdown as verification_markdown
from src.tracker.google import GoogleSheetError, download_xlsx
from src.tracker.workbook import EXPECTED_SHEETS, MAX_WORKBOOK_BYTES, report_markdown

log = logging.getLogger(__name__)
NO_MENTIONS = discord.AllowedMentions.none()
DATA_SHEETS = [s for s in EXPECTED_SHEETS if s != "DASHBOARD"]
SHEET_CHOICES = [app_commands.Choice(name=s, value=s) for s in DATA_SHEETS]
ADD_CHOICES = [app_commands.Choice(name=s, value=s) for s in DATA_SHEETS if s != "CHANGELOG"]


def _row_line(r: RowView, *, sheet: bool = False) -> str:
    where = f" ({r.sheet})" if sheet else ""
    status = f" · {_md(r.status)}" if r.status else ""
    origin = " · ajouté par Samae" if r.origin == "samae" else ""
    return f"`{_md(r.key)}`{where} {_md(limits.truncate(r.title, 120))}{status}{origin}"


def _change_line(c: TrackerChange) -> str:
    what = "ajout" if c.kind == "add" else f"modification de {c.row_key}"
    detail = ", ".join(f"{k} → {v}" for k, v in c.payload.items() if v not in (None, ""))
    state = {"pending": "en attente", "applied": f"appliquée ({c.decider})", "rejected": f"refusée ({c.decider})"}[c.status]
    return f"#{c.id} · {c.sheet} · {what} · {_md(limits.truncate(detail, 160))} · par {_md(c.proposer)} · {state}"


VERIFY_SHOWN = 12  # constats affichés dans l'embed (le rapport complet est joint)


def verification_embed(report: Report) -> discord.Embed:
    title = "Classeur cohérent" if report.ok and not report.warnings else ("Classeur à corriger" if not report.ok else "Classeur à revoir")
    embed = make_embed(f"Vérification · {title}", f"{report.summary()}\nSource : {_md(report.source)}. Rien n'a été modifié.")
    for level, label in ((Level.ERROR, "Erreurs"), (Level.WARNING, "Avertissements")):
        items = [f for f in report.findings if f.level is level]
        if not items:
            continue
        lines = [limits.truncate(finding_line(f), 300) for f in items[:VERIFY_SHOWN]]
        if len(items) > VERIFY_SHOWN:
            lines.append(f"… et {len(items) - VERIFY_SHOWN} autre(s) : voir le rapport joint.")
        add_field(embed, f"{label} ({len(items)})", "\n".join(lines))
    return embed


def _item_line(item) -> str:
    kind = "PR" if item.kind == "pull" else "commit"
    state = {"merged": " · fusionnée", "closed": " · fermée", "open": " · ouverte"}.get(item.state, "")
    link = f"[{kind} {item.label}]({item.url})" if item.url else f"{kind} {item.label}"
    when = f" · {item.occurred_at[:10]}" if item.occurred_at else ""
    return f"{link} {_md(limits.truncate(item.title, 120))} · {_md(item.author) or '?'}{when}{state}"


def sync_embed(result: SyncResult) -> discord.Embed:
    embed = make_embed(
        f"Synchronisation GitHub · {result.repository}",
        f"Branche {result.branch} : {result.new_commits} nouveau(x) commit(s) sur {result.commits_read} lu(s), "
        f"{result.new_pulls} nouvelle(s) PR, {result.newly_merged} PR fusionnée(s) depuis la dernière fois. Rien n'est écrit sur GitHub.",
    )
    if not result.tracker_loaded:
        add_field(embed, "Suivi", "Aucun classeur importé : importez-le (/suivi importer) pour relier le code aux fonctionnalités.")
    if result.touched:
        lines = [f"`{_md(k)}` : {', '.join(v[:4])}" + (f" (+{len(v) - 4})" if len(v) > 4 else "") for k, v in sorted(result.touched.items())]
        add_field(embed, f"Éléments du suivi cités ({len(result.touched)})", "\n".join(lines))
    if result.proposals:
        lines = [_change_line(c) for c in result.proposals]
        add_field(embed, f"Propositions à valider ({len(result.proposals)})", "\n".join(lines) + "\nValidez ou refusez avec /suivi valider et /suivi refuser.")
    if result.unvalidated:
        add_field(embed, "Code sur des éléments non validés", ", ".join(result.unvalidated) + " : statut inchangé (décision de l'équipe d'abord).")
    if result.unknown:
        add_field(embed, "Identifiants inconnus du suivi", ", ".join(result.unknown[:20]))
    return embed


async def key_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    bot: SaeBot = interaction.client  # type: ignore[assignment]
    sheet = getattr(interaction.namespace, "onglet", None)
    if not sheet:
        return []
    text = current.strip().casefold()
    rows = [r for r in bot.tracker.rows(sheet) if not text or text in r.key.casefold() or text in r.title.casefold()]
    return [app_commands.Choice(name=limits.truncate(f"{r.key} · {r.title}", 100), value=r.key[:100]) for r in rows[:25]]


async def code_key_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    """Lignes qui ont un avancement (FEATURES, GAMEPLAY, UI_UX, ART_DIRECTION), pour /suivi code."""
    bot: SaeBot = interaction.client  # type: ignore[assignment]
    text = current.strip().casefold()
    rows = [r for sheet in PROGRESS_SHEETS for r in bot.tracker.rows(sheet)
            if not text or text in r.key.casefold() or text in r.title.casefold()]
    return [app_commands.Choice(name=limits.truncate(f"{r.key} · {r.title}", 100), value=r.key[:100]) for r in rows[:25]]


async def status_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    bot: SaeBot = interaction.client  # type: ignore[assignment]
    sheet = getattr(interaction.namespace, "onglet", None) or FEATURES
    values = bot.tracker.allowed_statuses(sheet)
    return [app_commands.Choice(name=v[:100], value=v[:100]) for v in values if current.casefold() in v.casefold()][:25]


async def change_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[int]]:
    bot: SaeBot = interaction.client  # type: ignore[assignment]
    return [app_commands.Choice(name=limits.truncate(_change_line(c), 100), value=c.id) for c in bot.tracker.pending()][:25]


class TrackerCog(commands.Cog):
    suivi = app_commands.Group(name="suivi", description="Suivi du projet : classeur GAME DESIGN DATABASE (consulter, proposer)",
                               guild_only=True)

    def __init__(self, bot: SaeBot) -> None:
        self.bot = bot

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return require_team_space(interaction)

    @property
    def service(self):
        return self.bot.tracker

    def _actor(self, interaction: discord.Interaction) -> Actor:
        user = interaction.user
        admin = isinstance(user, discord.Member) and is_bot_admin(user, self.bot.settings.admin_role_name)
        return Actor(user.id, (getattr(user, "display_name", None) or str(user))[:100], admin, interaction.guild_id)

    async def _fail(self, interaction: discord.Interaction, exc: MeetingError) -> None:
        await send_error(interaction, str(exc), title=exc.title, tone="warning")

    async def _list(self, interaction: discord.Interaction, title: str, lines: list[str], *, empty: str) -> None:
        chunks = limits.chunk_lines(lines or [empty], limits.EMBED_DESCRIPTION - 100)
        pages = [make_embed(title, c, footer=f"Page {i}/{len(chunks)} · Samae") for i, c in enumerate(chunks, 1)]
        if len(pages) == 1:
            await interaction.response.send_message(embed=pages[0], ephemeral=True)
            return
        view = PagesView(pages, interaction.user.id)
        await interaction.response.send_message(embed=pages[0], view=view, ephemeral=True)
        view.original = interaction

    # --- Import / export -----------------------------------------------------------------

    @suivi.command(name="importer", description="Admins : importer le classeur Excel (aperçu, sauvegarde, puis confirmation)",
                   extras={"admin": True})
    @app_commands.describe(fichier="Le fichier « GAME DESIGN DATABASE — LUNASTEH.xlsx » (il ne sera jamais modifié)")
    @admin_only()
    async def import_workbook(self, interaction: discord.Interaction, fichier: discord.Attachment) -> None:
        actor = self._actor(interaction)
        if not fichier.filename.lower().endswith((".xlsx", ".xlsm")):
            await send_error(interaction, "Envoyez un fichier Excel .xlsx.", tone="warning")
            return
        if fichier.size > MAX_WORKBOOK_BYTES:
            await send_error(interaction, f"Fichier trop gros (maximum {MAX_WORKBOOK_BYTES // 1024 // 1024} Mio).", tone="warning")
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            data = await fichier.read()
            info, rows, diff = self.service.preview_import(data, fichier.filename)
        except (MeetingError, discord.HTTPException) as exc:
            await interaction.followup.send(embed=make_embed("Import impossible", str(exc)), ephemeral=True)
            return
        embed = make_embed(f"Aperçu de l'import · {fichier.filename}", "Rien n'est encore enregistré.")
        sheets = [f"{s.name} : {s.data_rows} ligne(s), id « {s.id_column or '—'} », statut « {s.status_column or '—'} »"
                  for s in info.sheets if s.header_row]
        add_field(embed, "Onglets lus", "\n".join(sheets) or "aucun")
        add_field(embed, "Changements", f"{diff['new']} nouvelle(s) ligne(s), {diff['changed']} modifiée(s), {diff['unchanged']} identique(s), "
                  f"{diff['kept']} gardée(s) telles que modifiées dans Samae, {diff['missing']} absente(s) du fichier (conservées, rien n'est supprimé)")
        if info.warnings:
            add_field(embed, "Avertissements", "\n".join(f"• {w}" for w in info.warnings))
        add_field(embed, "Sécurité", "La base est sauvegardée avant l'import ; une copie de ce fichier est gardée telle quelle.")

        async def confirmed(confirm_interaction: discord.Interaction) -> None:
            try:
                result = self.service.apply_import(data, fichier.filename, actor)
            except MeetingError as exc:
                await confirm_interaction.edit_original_response(content=str(exc), embeds=[], view=None)
                return
            self.bot.record_admin_action(confirm_interaction, "suivi importer", fichier.filename,
                                         f"{result['new']} nouvelles, {result['changed']} modifiées")
            await confirm_interaction.edit_original_response(
                content=f"Import terminé : {result['new']} nouvelle(s) ligne(s), {result['changed']} modifiée(s), "
                        f"{result['kept']} gardée(s) (modifiées dans Samae).", embeds=[], view=None,
            )

        view = ConfirmView(interaction.user.id, confirmed, command="/suivi importer")
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        view.original = interaction

    @suivi.command(name="analyse", description="Admins : rapport détaillé du classeur importé (colonnes, formules, statuts)",
                   extras={"admin": True})
    @admin_only()
    async def analysis(self, interaction: discord.Interaction) -> None:
        try:
            info = self.service.require_info()
        except MeetingError as exc:
            await self._fail(interaction, exc)
            return
        text = report_markdown(info)
        file = discord.File(io.BytesIO(text.encode("utf-8")), filename="analyse-classeur.md")
        await interaction.response.send_message("Analyse du dernier classeur importé (aucune donnée de ligne, seulement la structure).",
                                                file=file, ephemeral=True)

    @suivi.command(name="exporter", description="Admins : nouvelle copie Excel à jour (formules conservées, original intact)",
                   extras={"admin": True})
    @admin_only()
    async def export(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            result = self.service.export(self._actor(interaction))
        except MeetingError as exc:
            await interaction.followup.send(embed=make_embed("Export impossible", str(exc)), ephemeral=True)
            return
        notes = [f"{result.written_cells} cellule(s) écrite(s), {result.appended_rows} ligne(s) ajoutée(s), "
                 f"{result.skipped_formula_cells} cellule(s) à formule laissée(s) intacte(s)."] + result.warnings
        self.bot.record_admin_action(interaction, "suivi exporter", result.path.name)
        await interaction.followup.send("\n".join(notes), file=discord.File(result.path), ephemeral=True)

    # --- Vérification -------------------------------------------------------------------

    async def _verification_source(self, fichier: discord.Attachment | None) -> tuple[bytes, str]:
        """Fichier envoyé, sinon Google Sheet configuré (TRACKER_GOOGLE_SHEET), sinon dernier import."""
        if fichier is not None:
            if not fichier.filename.lower().endswith((".xlsx", ".xlsm")):
                raise TrackerError("Envoyez un fichier Excel .xlsx.")
            if fichier.size > MAX_WORKBOOK_BYTES:
                raise TrackerError(f"Fichier trop gros (maximum {MAX_WORKBOOK_BYTES // 1024 // 1024} Mio).")
            return await fichier.read(), fichier.filename
        sheet_id = self.bot.settings.tracker_google_sheet
        if sheet_id:
            return await download_xlsx(sheet_id), "Google Sheet de l'équipe"
        latest = self.service.latest_original()
        if latest is None:
            raise TrackerError("Aucun classeur à vérifier : joignez un fichier .xlsx, importez-le avec /suivi importer, "
                               "ou indiquez le Google Sheet dans TRACKER_GOOGLE_SHEET (.env).")
        data, name = latest
        return data, f"dernier import ({name})"

    @suivi.command(name="verifier", description="Vérifier la cohérence du classeur (statuts, renvois, décisions, DASHBOARD…)")
    @app_commands.describe(fichier="Facultatif : un fichier .xlsx (sinon le Google Sheet configuré, ou le dernier import)")
    async def verify(self, interaction: discord.Interaction, fichier: discord.Attachment | None = None) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            data, label = await self._verification_source(fichier)
            report = await asyncio.to_thread(lambda: verify_workbook(sheets_from_xlsx(data), source=label))
        except (TrackerError, GoogleSheetError, ValueError, discord.HTTPException) as exc:
            await interaction.followup.send(embed=make_embed("Vérification impossible", str(exc)), ephemeral=True)
            return
        except Exception:  # fichier corrompu ou format inattendu
            log.exception("Vérification du classeur impossible")
            await interaction.followup.send(
                embed=make_embed("Vérification impossible", "Classeur illisible : est-ce bien un fichier .xlsx ?"), ephemeral=True
            )
            return
        file = discord.File(io.BytesIO(verification_markdown(report).encode("utf-8")), filename="verification-classeur.md")
        await interaction.followup.send(embed=verification_embed(report), file=file, ephemeral=True)

    # --- Consultation ---------------------------------------------------------------------

    @suivi.command(name="progression", description="Avancement : fonctionnalités terminées, statuts, questions ouvertes")
    async def progress(self, interaction: discord.Interaction) -> None:
        try:
            p = self.service.progress()
        except MeetingError as exc:
            await self._fail(interaction, exc)
            return
        embed = make_embed("Avancement du projet", f"**{p['done']}/{p['features']}** fonctionnalités terminées (**{p['percent']} %**).")
        add_field(embed, "Fonctionnalités par statut", "\n".join(f"{_md(k)} : {v}" for k, v in p["by_status"].items()) or "aucune")
        add_field(embed, "Classeur", f"Idées : {p['ideas']} · décisions : {p['decisions']} · questions ouvertes : {p['open_questions']}")
        if p["pending_changes"]:
            add_field(embed, "En attente de validation", f"{p['pending_changes']} proposition(s) : `/suivi en-attente`")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @suivi.command(name="chercher", description="Rechercher dans tout le classeur (ou un onglet)")
    @app_commands.choices(onglet=SHEET_CHOICES)
    async def search(self, interaction: discord.Interaction, texte: app_commands.Range[str, 2, 100],
                     onglet: app_commands.Choice[str] | None = None) -> None:
        try:
            rows = self.service.search(texte, sheet=onglet.value if onglet else None)
        except MeetingError as exc:
            await self._fail(interaction, exc)
            return
        await self._list(interaction, f"Recherche « {texte} »", [_row_line(r, sheet=True) for r in rows], empty="Aucun résultat.")

    @suivi.command(name="fonctionnalites", description="Fonctionnalités (toutes, restantes, ou d'un statut)")
    @app_commands.autocomplete(statut=status_autocomplete)
    async def features(self, interaction: discord.Interaction, restantes: bool = True, statut: str | None = None) -> None:
        try:
            rows = self.service.by_status(FEATURES, status=statut, remaining=restantes and not statut)
        except MeetingError as exc:
            await self._fail(interaction, exc)
            return
        title = f"Fonctionnalités{' restantes' if restantes and not statut else ''}{f' · {statut}' if statut else ''} ({len(rows)})"
        await self._list(interaction, title, [_row_line(r) for r in rows], empty="Aucune fonctionnalité.")

    @suivi.command(name="decisions", description="Décisions officielles enregistrées (dernières d'abord)")
    async def decisions(self, interaction: discord.Interaction) -> None:
        await self._sheet(interaction, DECISIONS, "Décisions")

    @suivi.command(name="idees", description="Idées du classeur")
    async def ideas(self, interaction: discord.Interaction) -> None:
        await self._sheet(interaction, IDEAS, "Idées du classeur")

    @suivi.command(name="questions", description="Questions ouvertes")
    async def questions(self, interaction: discord.Interaction) -> None:
        try:
            rows = self.service.by_status(OPEN_QUESTIONS, open_only=True)
        except MeetingError as exc:
            await self._fail(interaction, exc)
            return
        await self._list(interaction, f"Questions ouvertes ({len(rows)})", [_row_line(r) for r in rows], empty="Aucune question ouverte.")

    async def _sheet(self, interaction: discord.Interaction, sheet: str, title: str) -> None:
        try:
            self.service.require_info()
            rows = self.service.latest(sheet, limit=200)
        except MeetingError as exc:
            await self._fail(interaction, exc)
            return
        await self._list(interaction, f"{title} ({len(rows)})", [_row_line(r) for r in rows], empty="Aucune ligne.")

    @suivi.command(name="voir", description="Toutes les colonnes d'une ligne du classeur")
    @app_commands.choices(onglet=SHEET_CHOICES)
    @app_commands.autocomplete(cle=key_autocomplete)
    async def show(self, interaction: discord.Interaction, onglet: app_commands.Choice[str], cle: str) -> None:
        row = self.service.repo.get_row(onglet.value, cle)
        if row is None:
            await send_error(interaction, f"Ligne introuvable : {onglet.value} / {cle}.", tone="warning")
            return
        view = self.service.view(row)
        embed = make_embed(f"{onglet.value} · {cle}", _md(view.title))
        for header, value in list(row.data.items())[:24]:
            add_field(embed, header, _md(str(value)) if value not in (None, "") else "—", inline=len(str(value or "")) < 40)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # --- Synchronisation avec le dépôt GitHub ------------------------------------------------

    @suivi.command(name="synchroniser", description="Admins : relire le dépôt GitHub et proposer les statuts (EN COURS, TERMINÉ)",
                   extras={"admin": True})
    @admin_only()
    async def synchronize(self, interaction: discord.Interaction) -> None:
        sync = self.bot.repo_sync
        if not sync.enabled:
            await send_error(interaction, "Synchronisation non configurée : indiquez GITHUB_SYNC_REPOSITORY=propriétaire/dépôt "
                             "dans .env (voir docs/synchronisation-github.md).", tone="warning")
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            result = await sync.sync()
        except GitHubSyncError as exc:
            await interaction.followup.send(embed=make_embed("Synchronisation impossible", str(exc)), ephemeral=True)
            return
        self.bot.record_admin_action(interaction, "suivi synchroniser", f"{len(result.proposals)} proposition(s)")
        await interaction.followup.send(embed=sync_embed(result), ephemeral=True)

    @suivi.command(name="code", description="Commits et pull requests GitHub qui citent une ligne du suivi")
    @app_commands.describe(cle="Identifiant cité dans les commits ou les PR, par exemple FEAT-002")
    @app_commands.autocomplete(cle=code_key_autocomplete)
    async def code(self, interaction: discord.Interaction, cle: str) -> None:
        sync = self.bot.repo_sync
        if not sync.enabled:
            await send_error(interaction, "Synchronisation GitHub non configurée (GITHUB_SYNC_REPOSITORY).", tone="warning")
            return
        key = cle.strip().upper()
        items = sync.mentioning(key)
        lines = [_item_line(i) for i in items]
        last = sync.last_run()
        empty = (f"Aucun commit ni aucune pull request ne cite {key}" + (" depuis la dernière synchronisation." if last else
                 " : la synchronisation n'a encore jamais été lancée (/suivi synchroniser)."))
        await self._list(interaction, f"Code lié à {key} ({len(items)})", lines, empty=empty)

    # --- Modifications -----------------------------------------------------------------

    @suivi.command(name="ajouter", description="Ajouter une ligne (idées et questions : direct ; le reste : validation)")
    @app_commands.choices(onglet=ADD_CHOICES)
    @app_commands.autocomplete(statut=status_autocomplete)
    async def add(self, interaction: discord.Interaction, onglet: app_commands.Choice[str], texte: app_commands.Range[str, 1, 500],
                  description: app_commands.Range[str, 0, 500] | None = None, statut: str | None = None) -> None:
        try:
            change = self.service.propose_add(onglet.value, self._actor(interaction), title=texte, description=description or "",
                                              status=statut or "")
        except MeetingError as exc:
            await self._fail(interaction, exc)
            return
        if change.status == "applied":
            text = f"Ligne {change.row_key} ajoutée à {onglet.value}."
        else:
            text = (f"Proposition #{change.id} enregistrée : un administrateur doit la valider (`/suivi valider proposition:{change.id}`)."
                    + (" Une décision officielle n'est jamais ajoutée sans validation humaine." if onglet.value == DECISIONS else ""))
        await interaction.response.send_message(embed=success_embed(text), ephemeral=True)

    @suivi.command(name="statut", description="Proposer un nouveau statut pour une ligne (validé par un administrateur)")
    @app_commands.choices(onglet=SHEET_CHOICES)
    @app_commands.autocomplete(cle=key_autocomplete, statut=status_autocomplete)
    async def status(self, interaction: discord.Interaction, onglet: app_commands.Choice[str], cle: str, statut: str,
                     motif: app_commands.Range[str, 0, 300] | None = None) -> None:
        try:
            info = self.service.sheet_info(onglet.value)
            if info is None or not info.status_column:
                raise TrackerError(f"L'onglet {onglet.value} n'a pas de colonne de statut.")
            change = self.service.propose_update(onglet.value, cle, self._actor(interaction), info.status_column, statut, motif or "")
        except MeetingError as exc:
            await self._fail(interaction, exc)
            return
        await interaction.response.send_message(embed=success_embed(
            f"Proposition #{change.id} : {onglet.value} {cle} → « {statut} ». En attente de validation (`/suivi en-attente`)."
        ), ephemeral=True)

    @suivi.command(name="en-attente", description="Propositions de modification en attente de validation")
    async def pending(self, interaction: discord.Interaction) -> None:
        changes = self.service.pending()
        await self._list(interaction, f"En attente de validation ({len(changes)})", [_change_line(c) for c in changes],
                         empty="Aucune proposition en attente.")

    @suivi.command(name="historique", description="Dernières modifications appliquées ou refusées")
    async def history(self, interaction: discord.Interaction) -> None:
        changes = self.service.history(25)
        await self._list(interaction, "Historique du suivi", [_change_line(c) for c in changes], empty="Aucune modification.")

    @suivi.command(name="valider", description="Admins : appliquer une proposition (sauvegarde puis confirmation)", extras={"admin": True})
    @app_commands.autocomplete(proposition=change_autocomplete)
    @admin_only()
    async def approve(self, interaction: discord.Interaction, proposition: int) -> None:
        actor = self._actor(interaction)
        change = self.service.repo.change(proposition)
        if change is None or change.status != "pending":
            await send_error(interaction, "Proposition introuvable ou déjà traitée.", tone="warning")
            return

        async def confirmed(confirm_interaction: discord.Interaction) -> None:
            try:
                applied = self.service.approve(change.id, actor)
            except MeetingError as exc:
                await confirm_interaction.edit_original_response(content=str(exc), embeds=[], view=None)
                return
            self.bot.record_admin_action(confirm_interaction, "suivi valider", f"#{applied.id}", applied.sheet)
            await confirm_interaction.edit_original_response(content=f"Appliquée : {_change_line(applied)}", embeds=[], view=None)

        view = ConfirmView(interaction.user.id, confirmed, command="/suivi valider")
        before = ", ".join(f"{k} : « {v} »" for k, v in (change.before_data or {}).items())
        text = f"Appliquer la proposition ?\n{_change_line(change)}" + (f"\nValeur actuelle : {before}" if before else "")
        if change.reason:
            text += f"\nMotif : {_md(change.reason)}"
        await interaction.response.send_message(text[:1900], view=view, ephemeral=True, allowed_mentions=NO_MENTIONS)
        view.original = interaction

    @suivi.command(name="refuser", description="Refuser (admins) ou retirer (son auteur) une proposition en attente")
    @app_commands.autocomplete(proposition=change_autocomplete)
    async def reject(self, interaction: discord.Interaction, proposition: int) -> None:
        try:
            self.service.reject(proposition, self._actor(interaction))
        except MeetingError as exc:
            await self._fail(interaction, exc)
            return
        await interaction.response.send_message(embed=success_embed(f"Proposition #{proposition} refusée : rien n'a été modifié."), ephemeral=True)


async def setup(bot: SaeBot) -> None:
    await bot.add_cog(TrackerCog(bot))
