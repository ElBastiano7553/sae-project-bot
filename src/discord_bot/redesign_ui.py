"""Interface de /refonte : embeds paginés et boutons (Analyser, Prévisualiser, Appliquer, Annuler).

La vue n'est qu'une télécommande : chaque bouton appelle le cog, qui revérifie
l'auteur, ses permissions, celles du bot et l'état du serveur avant toute action.
Elle n'est pas persistante : après un redémarrage du bot ou 10 minutes sans
action, il suffit de relancer /refonte (rien n'a été modifié entre-temps).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import discord

from src.discord_bot.embeds import add_field, fit_embed, make_embed
from src.services import design
from src.services import text_limits as limits
from src.services.design import Tone
from src.services.server_redesign import (
    RedesignOptions,
    RedesignOutcome,
    RedesignPlan,
    ResultStatus,
    ServerAnalysis,
    OpKind,
    outcome_sections,
    plan_sections,
    summary_lines,
)

log = logging.getLogger(__name__)

VIEW_TIMEOUT = 600
PAGE_SIZE = 3500  # caractères par page (limite Discord : 4096 par description)

ANALYSIS, PREVIEW, CONFIRM, RUNNING, DONE, CLOSED = "analysis", "preview", "confirm", "running", "done", "closed"


@dataclass
class RedesignSession:
    """État d'une refonte en cours de consultation (une par commande /refonte)."""

    author_id: int
    options: RedesignOptions
    plan: RedesignPlan
    analysis: ServerAnalysis
    context: list[str] = field(default_factory=list)  # état de Samae (associations, réunions…)
    mode: str = ANALYSIS
    page: int = 0
    notice: str = ""  # message affiché en tête de la prochaine page (ex. « le serveur a changé »)
    outcome: RedesignOutcome | None = None
    outcome_extra: list[discord.Embed] = field(default_factory=list)


def _pages_from_sections(sections: list[tuple[str, list[str]]]) -> list[str]:
    lines: list[str] = []
    for title, items in sections:
        lines.append(f"**{design.escape_markdown(title)}**")
        lines.extend(f"• {design.escape_markdown(item)}" for item in items)
        lines.append("")
    return limits.chunk_lines(lines, PAGE_SIZE) or ["*Rien à afficher.*"]


def analysis_embed(session: RedesignSession) -> discord.Embed:
    a, plan = session.analysis, session.plan
    tone = Tone.FAILURE if plan.blockers else Tone.INFO
    embed = make_embed(
        "Refonte du serveur · analyse",
        (f"{session.notice}\n\n" if session.notice else "")
        + "Analyse terminée : **rien n'a été modifié**. Consultez le plan avec « Prévisualiser », "
        "puis « Appliquer la refonte » demande une confirmation explicite.",
        tone=tone,
    )
    add_field(
        embed,
        "Serveur",
        f"{design.plural(a.categories, 'catégorie')} · {design.plural(a.text_channels, 'salon textuel', 'salons textuels')} · "
        f"{design.plural(a.voice_channels, 'salon vocal', 'salons vocaux')}"
        + (f" · {a.other_channels} autre(s)" if a.other_channels else "")
        + f" · {design.plural(a.roles, 'rôle')}",
    )
    if session.context:
        add_field(embed, "Samae", "\n".join(session.context))
    perms = "Toutes les permissions utiles sont accordées." if not a.missing_bot_permissions else (
        "Permissions absentes : " + ", ".join(a.missing_bot_permissions) + ". « Gérer les rôles » n'est utile qu'avec roles:True."
    )
    add_field(embed, "Permissions du bot", perms)
    add_field(embed, "Plan proposé", "\n".join(summary_lines(plan)))
    attention = list(plan.blockers)
    if plan.risks:
        attention.append(f"{len(plan.risks)} point(s) d'attention : voir « Risques détectés » dans l'aperçu")
    if plan.of(OpKind.MANUAL):
        attention.append(f"{len(plan.of(OpKind.MANUAL))} intervention(s) manuelle(s) : voir l'aperçu")
    if attention:
        add_field(embed, "Avant d'appliquer", "\n".join(f"• {line}" for line in attention))
    options = []
    options.append("salons facultatifs créés" if session.options.create_optional else "salons facultatifs non créés (option facultatifs)")
    options.append("rôles de projet créés" if session.options.create_roles else "rôles non créés (option roles)")
    embed.set_footer(text=design.footer("refonte", " · ".join(options), f"plan {plan.fingerprint}"))
    return fit_embed(embed)


def plan_embeds(session: RedesignSession) -> list[discord.Embed]:
    plan = session.plan
    pages = _pages_from_sections(plan_sections(plan))
    embeds = []
    for number, text in enumerate(pages, start=1):
        header = f"{session.notice}\n\n" if session.notice and number == 1 else ""
        embed = make_embed(
            "Refonte du serveur · aperçu du plan",
            header + text,
            tone=Tone.FAILURE if plan.blockers else Tone.INFO,
            footer=design.footer("aperçu sans effet", f"page {number}/{len(pages)}", f"plan {plan.fingerprint}"),
        )
        embeds.append(fit_embed(embed))
    return embeds


def confirm_embed(session: RedesignSession) -> discord.Embed:
    plan = session.plan
    embed = make_embed(
        "Confirmer la refonte ?",
        "Samae va exécuter les opérations ci-dessous, dans cet ordre, après une sauvegarde de sa base de données. "
        "**Aucun salon, aucune catégorie, aucun rôle et aucun message ne sera supprimé.** "
        "Juste avant d'agir, Samae revérifie vos permissions, les siennes et l'état du serveur : "
        "si quelque chose a changé depuis l'aperçu, un nouveau plan vous est présenté au lieu d'être appliqué.",
        tone=Tone.WARNING,
    )
    add_field(embed, "Opérations", "\n".join(summary_lines(plan)))
    add_field(
        embed,
        "Retour en arrière",
        "Les renommages et déplacements se défont à la main (anciens noms dans le journal de la refonte). "
        "Discord ne permet pas d'annuler automatiquement une création : un salon créé par erreur se supprime à la main.",
    )
    embed.set_footer(text=design.footer("refonte", f"plan {plan.fingerprint}"))
    return fit_embed(embed)


def running_embed(text: str = "Vérification de l'état du serveur, puis application…") -> discord.Embed:
    return make_embed("Refonte en cours", text, tone=Tone.INFO)


def outcome_embeds(session: RedesignSession) -> list[discord.Embed]:
    outcome = session.outcome
    assert outcome is not None
    failed = outcome.with_status(ResultStatus.FAILED)
    if failed:
        title, tone = "Refonte partiellement appliquée", Tone.FAILURE
        intro = f"{len(failed)} opération(s) ont échoué : la refonte n'est **pas** terminée. Les autres ont été appliquées."
    elif not outcome.complete:
        title, tone = "Refonte appliquée, avec des opérations ignorées", Tone.WARNING
        intro = "Toutes les opérations possibles ont été faites ; certaines ont été ignorées (détail ci-dessous)."
    else:
        title, tone = "Refonte appliquée", Tone.SUCCESS
        intro = "Toutes les opérations prévues ont réussi."
    notes = []
    notes.append(f"Sauvegarde de la base : `{outcome.backup}`" if outcome.backup else "Sauvegarde de la base : non applicable (base en mémoire)")
    if outcome.journal:
        notes.append(f"Journal : `{outcome.journal}` (dossier de la base, sous-dossier refonte)")
    notes.append("Relancez /refonte pour vérifier : un serveur conforme n'affiche plus aucune opération.")
    pages = _pages_from_sections(outcome_sections(outcome))
    embeds = []
    for number, text in enumerate(pages, start=1):
        head = f"{intro}\n" + "\n".join(notes) + "\n\n" if number == 1 else ""
        embeds.append(
            fit_embed(make_embed(title, head + text, tone=tone, footer=design.footer("rapport de refonte", f"page {number}/{len(pages)}")))
        )
    return embeds


class RedesignView(discord.ui.View):
    """Boutons de /refonte, recalculés à chaque changement d'étape."""

    def __init__(self, cog, session: RedesignSession) -> None:
        super().__init__(timeout=VIEW_TIMEOUT)
        self.cog = cog
        self.session = session
        self.original: discord.Interaction | None = None
        self.render()

    # --- Contenu ---------------------------------------------------------------------

    def pages(self) -> list[discord.Embed]:
        mode = self.session.mode
        if mode == PREVIEW:
            return plan_embeds(self.session)
        if mode == CONFIRM:
            return [confirm_embed(self.session)]
        if mode == DONE:
            return outcome_embeds(self.session) + self.session.outcome_extra
        return [analysis_embed(self.session)]

    def current_embed(self) -> discord.Embed:
        pages = self.pages()
        self.session.page = max(0, min(self.session.page, len(pages) - 1))
        return pages[self.session.page]

    def render(self) -> None:
        """Boutons adaptés à l'étape ; ceux qui ne servent pas sont masqués ou désactivés."""
        self.clear_items()
        mode = self.session.mode
        count = len(self.pages()) if mode in (PREVIEW, DONE) else 1
        if count > 1:
            self._add("Précédent", "prev", discord.ButtonStyle.secondary, 0, disabled=self.session.page == 0)
            self._add(f"{self.session.page + 1}/{count}", "noop", discord.ButtonStyle.secondary, 0, disabled=True)
            self._add("Suivant", "next", discord.ButtonStyle.secondary, 0, disabled=self.session.page >= count - 1)
        if mode in (ANALYSIS, PREVIEW):
            can_apply = self.session.plan.can_apply
            self._add("Analyser le serveur", "analyse", discord.ButtonStyle.secondary, 1)
            if mode == ANALYSIS:
                self._add("Prévisualiser", "preview", discord.ButtonStyle.primary, 1)
            self._add("Appliquer la refonte", "apply", discord.ButtonStyle.danger, 1, disabled=not can_apply)
            self._add("Annuler", "cancel", discord.ButtonStyle.secondary, 1)
        elif mode == CONFIRM:
            self._add("Confirmer l'application", "confirm", discord.ButtonStyle.danger, 1)
            self._add("Retour à l'aperçu", "preview", discord.ButtonStyle.secondary, 1)
            self._add("Annuler", "cancel", discord.ButtonStyle.secondary, 1)

    def _add(self, label: str, action: str, style: discord.ButtonStyle, row: int, *, disabled: bool = False) -> None:
        button = discord.ui.Button(label=label, style=style, row=row, disabled=disabled)
        button.callback = self._callback(action)
        self.add_item(button)

    def _callback(self, action: str):
        async def callback(interaction: discord.Interaction) -> None:
            await self.cog.handle_redesign_button(interaction, self, action)

        return callback

    async def show(self, interaction: discord.Interaction) -> None:
        """Redessine le message de la refonte en réponse à un clic."""
        self.render()
        embed = self.current_embed()
        if self.session.mode in (DONE, CLOSED) and len(self.pages()) <= 1:
            self.stop()
        view = self if self.children else None
        if interaction.response.is_done():
            await interaction.edit_original_response(content=None, embed=embed, view=view)
        else:
            await interaction.response.edit_message(content=None, embed=embed, view=view)

    # --- Sécurité et expiration ----------------------------------------------------------------

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.session.author_id:
            await interaction.response.send_message(
                "Seule la personne qui a lancé /refonte peut utiliser ces boutons.", ephemeral=True
            )
            return False
        if self.session.mode == RUNNING:
            await interaction.response.send_message("La refonte est en cours d'application : patientez.", ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        if self.original is None or self.session.mode in (DONE, CLOSED, RUNNING):
            return
        try:
            await self.original.edit_original_response(
                content="Délai écoulé (10 minutes) : rien n'a été modifié. Relancez /refonte pour une nouvelle analyse.",
                embed=None,
                view=None,
            )
        except discord.HTTPException:
            log.debug("Impossible de signaler l'expiration de /refonte", exc_info=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        log.error("Erreur dans l'interface de /refonte", exc_info=error)
        message = "Une erreur inattendue est survenue. Rien d'autre ne sera fait ; consultez /admin history et les journaux du bot."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            pass
