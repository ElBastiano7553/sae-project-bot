"""Interface de /refonte : étapes, pages et boutons.

Étapes : analyse (profil et style au choix) → aperçu paginé → confirmation →
application → résultat ; puis, seulement avec suppressions:True, examen des
suppressions → confirmation distincte → résultat. Le retour arrière
(/refonte retour) suit les mêmes étapes.

La vue n'est qu'une télécommande : chaque bouton appelle le cog, qui revérifie
l'auteur, ses permissions, celles de Samae et l'état du serveur avant toute
action. Elle n'est pas persistante : après un redémarrage du bot ou 10 minutes
sans action, il suffit de relancer /refonte (rien n'a été modifié entre-temps).
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
    OpKind,
    RedesignOptions,
    RedesignOutcome,
    RedesignPlan,
    ResultStatus,
    ServerAnalysis,
    describe,
    outcome_sections,
    plan_sections,
    summary_lines,
)

log = logging.getLogger(__name__)

VIEW_TIMEOUT = 600
PAGE_SIZE = 3500  # caractères par page (limite Discord : 4096 par description)

ANALYSIS, PREVIEW, CONFIRM, RUNNING, DONE, DELETIONS, DELETE_CONFIRM, CLOSED = (
    "analysis", "preview", "confirm", "running", "done", "deletions", "delete_confirm", "closed",
)
REFONTE, RETOUR = "refonte", "retour"


@dataclass
class RedesignSession:
    """État d'une refonte (ou d'un retour arrière) en cours de consultation."""

    author_id: int
    options: RedesignOptions
    plan: RedesignPlan
    analysis: ServerAnalysis | None
    context: list[str] = field(default_factory=list)  # état de Samae (associations, réunions…)
    mode: str = ANALYSIS
    page: int = 0
    notice: str = ""  # message affiché en tête de la prochaine page (ex. « le serveur a changé »)
    outcome: RedesignOutcome | None = None
    outcome_extra: list[discord.Embed] = field(default_factory=list)
    kind: str = REFONTE
    journal: dict | None = None  # retour arrière : journal de la refonte à annuler
    choices: dict[str, list[tuple[str, str]]] = field(default_factory=dict)  # profils et styles proposés
    applied: bool = False  # une application a eu lieu : le bouton Confirmer ne revient pas


def _pages_from_sections(sections: list[tuple[str, list[str]]]) -> list[str]:
    lines: list[str] = []
    for title, items in sections:
        lines.append(f"**{design.escape_markdown(title)}**")
        lines.extend(f"• {design.escape_markdown(item)}" for item in items)
        lines.append("")
    return limits.chunk_lines(lines, PAGE_SIZE) or ["*Rien à afficher.*"]


def _title(session: RedesignSession, step: str) -> str:
    return f"{'Retour arrière' if session.kind == RETOUR else 'Refonte du serveur'} · {step}"


def analysis_embed(session: RedesignSession) -> discord.Embed:
    plan = session.plan
    tone = Tone.FAILURE if plan.blockers else Tone.INFO
    intro = (f"{session.notice}\n\n" if session.notice else "") + (
        "Analyse terminée : **rien n'a été modifié**. Choisissez le profil et le style, consultez le plan avec "
        "« Prévisualiser », puis « Appliquer les changements » demande une confirmation explicite."
        if session.kind == REFONTE else
        "Retour arrière préparé : **rien n'a été modifié**. Seul ce qui est encore dans l'état laissé par la refonte "
        "sera rétabli ; les suppressions éventuelles se confirment séparément."
    )
    embed = make_embed(_title(session, "analyse"), intro, tone=tone)
    a = session.analysis
    if a is not None:
        add_field(
            embed,
            "Serveur actuel",
            f"{design.plural(a.categories, 'catégorie')} · {design.plural(a.text_channels, 'salon textuel', 'salons textuels')} · "
            f"{design.plural(a.voice_channels, 'salon vocal', 'salons vocaux')}"
            + (f" · {a.other_channels} forum(s) ou autre(s)" if a.other_channels else "")
            + f" · {design.plural(a.roles, 'rôle')}" + (f" dont {a.integration_roles} d'intégration" if a.integration_roles else "")
            + ("\nFonction Communauté : activée" if a.community else "\nFonction Communauté : non activée"),
        )
        perms = "Toutes les permissions utiles sont accordées." if not a.missing_bot_permissions else (
            "Permissions absentes : " + ", ".join(a.missing_bot_permissions) + "."
        )
        add_field(embed, "Permissions de Samae", perms)
    if session.context:
        add_field(embed, "Samae", "\n".join(session.context))
    if plan.target is not None:
        profile = plan.target.profile
        add_field(embed, "Cible", f"Profil : **{profile.label if profile else 'par défaut'}**"
                  + (f" ({profile.description})" if profile and profile.description else "")
                  + f"\nStyle : **{plan.target.style.label}**")
    add_field(embed, "Plan proposé", "\n".join(summary_lines(plan)))
    attention = list(plan.blockers)
    if plan.risks:
        attention.append(f"{len(plan.risks)} point(s) d'attention : voir « Risques détectés » dans l'aperçu")
    manual = plan.of(OpKind.MANUAL)
    if manual:
        attention.append(f"{len(manual)} opération(s) impossibles pour Samae : voir l'aperçu")
    if attention:
        add_field(embed, "Avant d'appliquer", "\n".join(f"• {line}" for line in attention))
    options = session.options
    flags = [
        "facultatifs créés" if options.create_optional else "facultatifs non créés",
        "rôles gérés" if options.manage_roles else "rôles non gérés",
        "permissions corrigées" if options.manage_permissions else "permissions non modifiées",
        "suppressions examinables" if options.deletions else "suppressions désactivées",
    ]
    embed.set_footer(text=design.footer(session.kind, " · ".join(flags), f"plan {plan.fingerprint}"))
    return fit_embed(embed)


def plan_embeds(session: RedesignSession) -> list[discord.Embed]:
    plan = session.plan
    pages = _pages_from_sections(plan_sections(plan) if session.kind == REFONTE else _rollback_sections(plan))
    embeds = []
    for number, text in enumerate(pages, start=1):
        header = f"{session.notice}\n\n" if session.notice and number == 1 else ""
        embed = make_embed(
            _title(session, "aperçu"),
            header + text,
            tone=Tone.FAILURE if plan.blockers else Tone.INFO,
            footer=design.footer("aperçu sans effet", f"page {number}/{len(pages)}", f"plan {plan.fingerprint}"),
        )
        embeds.append(fit_embed(embed))
    return embeds


def _rollback_sections(plan: RedesignPlan) -> list[tuple[str, list[str]]]:
    sections = []
    actions = [describe(op) for op in plan.actions]
    sections.append((f"À rétablir ({len(actions)})", actions or ["Rien à rétablir."]))
    manual = plan.of(OpKind.MANUAL)
    if manual:
        sections.append((f"Laissé tel quel ({len(manual)})", [describe(op) for op in manual]))
    if plan.deletions:
        sections.append((f"Suppressions proposées ({len(plan.deletions)}) · sur confirmation séparée",
                         [describe(op) for op in plan.deletions]))
    sections.append(("Limites du retour arrière", list(plan.risks)))
    return sections


def confirm_embed(session: RedesignSession) -> discord.Embed:
    plan = session.plan
    embed = make_embed(
        "Confirmer l'application ?",
        "Samae va exécuter les opérations ci-dessous, dans cet ordre (rôles, catégories, salons, permissions, positions), "
        "après une sauvegarde de sa base et un relevé de l'état actuel du serveur. "
        "**Rien ne sera supprimé à cette étape.** Juste avant d'agir, Samae revérifie vos permissions, les siennes et "
        "l'état du serveur : si quelque chose a changé depuis l'aperçu, les différences vous sont présentées au lieu d'être appliquées.",
        tone=Tone.WARNING,
    )
    add_field(embed, "Opérations", "\n".join(summary_lines(plan)))
    add_field(
        embed,
        "Retour arrière",
        "`/refonte retour` rétablit les noms, sujets, déplacements, permissions, rôles et positions encore dans l'état "
        "laissé par la refonte, et propose (séparément) de supprimer ce qu'elle a créé et qui est resté vide.",
    )
    embed.set_footer(text=design.footer(session.kind, f"plan {plan.fingerprint}"))
    return fit_embed(embed)


def deletion_embeds(session: RedesignSession) -> list[discord.Embed]:
    plan = session.plan
    lines = [describe(op) for op in plan.deletions]
    head = (
        (f"{session.notice}\n\n" if session.notice else "")
        + "**Examen des suppressions.** Chaque élément ci-dessous a été retenu pour une raison précise ; aucun ne l'est "
        "simplement parce qu'il est absent de la structure cible. **Une suppression est définitive** : les messages d'un "
        "salon supprimé sont perdus, un rôle supprimé est retiré à tous ses membres. Au moment de supprimer, Samae "
        "revérifie chaque élément (même nom, toujours vide si c'était la raison) et conserve tout ce qui a changé."
    )
    pages = _pages_from_sections([(f"Éléments concernés ({len(lines)})", lines)])
    return [
        fit_embed(make_embed(_title(session, "suppressions proposées"), (head + "\n\n" if index == 0 else "") + text,
                             tone=Tone.WARNING, footer=design.footer("aucune suppression faite", f"page {index + 1}/{len(pages)}",
                                                                     f"liste {plan.deletion_fingerprint}")))
        for index, text in enumerate(pages)
    ]


def delete_confirm_embed(session: RedesignSession) -> discord.Embed:
    plan = session.plan
    counts = {kind: len(plan.of(kind)) for kind in (OpKind.DELETE_CHANNEL, OpKind.DELETE_CATEGORY, OpKind.DELETE_ROLE)}
    embed = make_embed(
        "Confirmer les suppressions ?",
        f"**{len(plan.deletions)} élément(s) seront supprimés définitivement** : {counts[OpKind.DELETE_CHANNEL]} salon(s), "
        f"{counts[OpKind.DELETE_CATEGORY]} catégorie(s), {counts[OpKind.DELETE_ROLE]} rôle(s). Une sauvegarde de la base et "
        "un relevé complet de ces éléments sont faits avant ; `/refonte retour` peut les recréer **vides**, sans leurs messages "
        "ni leurs membres.",
        tone=Tone.FAILURE,
    )
    embed.set_footer(text=design.footer(session.kind, f"liste {plan.deletion_fingerprint}"))
    return fit_embed(embed)


def running_embed(text: str = "Vérification de l'état du serveur, puis application…") -> discord.Embed:
    return make_embed("Application en cours", text, tone=Tone.INFO)


def outcome_embeds(session: RedesignSession) -> list[discord.Embed]:
    outcome = session.outcome
    assert outcome is not None
    failed = outcome.with_status(ResultStatus.FAILED)
    name = outcome.title
    if failed:
        title, tone = f"{name} : partiellement appliquée", Tone.FAILURE
        intro = f"{len(failed)} opération(s) ont échoué : ce n'est **pas** terminé. Les autres ont été appliquées."
    elif outcome.remaining:
        title, tone = f"{name} : appliquée, écarts restants", Tone.WARNING
        intro = "Toutes les opérations ont été envoyées, mais la vérification relève encore des écarts (détail ci-dessous)."
    elif not outcome.complete:
        title, tone = f"{name} : appliquée, avec des opérations ignorées", Tone.WARNING
        intro = "Toutes les opérations possibles ont été faites ; certaines ont été ignorées (détail ci-dessous)."
    else:
        title, tone = f"{name} : appliquée", Tone.SUCCESS
        intro = "Toutes les opérations prévues ont réussi."
    notes = [f"Sauvegarde de la base : `{outcome.backup}`" if outcome.backup else "Sauvegarde de la base : non applicable (base en mémoire)"]
    if outcome.journal:
        notes.append(f"Journal : `{outcome.journal}` (dossier de la base, sous-dossier refonte) · annulation : `/refonte retour`")
    pages = _pages_from_sections(outcome_sections(outcome))
    embeds = []
    for number, text in enumerate(pages, start=1):
        head = f"{intro}\n" + "\n".join(notes) + "\n\n" if number == 1 else ""
        embeds.append(fit_embed(make_embed(title, head + text, tone=tone,
                                           footer=design.footer("résultat", f"page {number}/{len(pages)}"))))
    return embeds


class RedesignView(discord.ui.View):
    """Boutons et menus de /refonte, recalculés à chaque changement d'étape."""

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
        if mode == DELETIONS:
            return deletion_embeds(self.session)
        if mode == DELETE_CONFIRM:
            return [delete_confirm_embed(self.session)]
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
        s = self.session
        mode = s.mode
        count = len(self.pages()) if mode in (PREVIEW, DONE, DELETIONS) else 1
        if count > 1:
            self._add("Précédent", "prev", discord.ButtonStyle.secondary, 0, disabled=s.page == 0)
            self._add(f"{s.page + 1}/{count}", "noop", discord.ButtonStyle.secondary, 0, disabled=True)
            self._add("Suivant", "next", discord.ButtonStyle.secondary, 0, disabled=s.page >= count - 1)
        if mode == ANALYSIS and s.kind == REFONTE:
            for row, (name, placeholder) in enumerate((("profile", "Profil du serveur"), ("style", "Style d'affichage")), start=1):
                choices = s.choices.get(name) or []
                if len(choices) > 1:
                    current = s.options.profile if name == "profile" else s.options.style
                    select = discord.ui.Select(
                        placeholder=placeholder,
                        options=[discord.SelectOption(label=label[:100], value=value, default=value == current) for value, label in choices[:25]],
                        row=row,
                    )
                    select.callback = self._select_callback(name, select)
                    self.add_item(select)
        if mode in (ANALYSIS, PREVIEW):
            self._add("Analyser le serveur", "analyse", discord.ButtonStyle.secondary, 3)
            if mode == ANALYSIS:
                self._add("Prévisualiser", "preview", discord.ButtonStyle.primary, 3)
            self._add("Appliquer les changements", "apply", discord.ButtonStyle.danger, 3, disabled=not s.plan.can_apply or s.applied)
            if s.plan.can_delete:
                self._add("Examiner les suppressions", "deletions", discord.ButtonStyle.secondary, 4)
            self._add("Annuler", "cancel", discord.ButtonStyle.secondary, 3)
        elif mode == CONFIRM:
            self._add("Confirmer l'application", "confirm", discord.ButtonStyle.danger, 3)
            self._add("Retour à l'aperçu", "preview", discord.ButtonStyle.secondary, 3)
            self._add("Annuler", "cancel", discord.ButtonStyle.secondary, 3)
        elif mode == DELETIONS:
            self._add("Supprimer ces éléments", "delete_review", discord.ButtonStyle.danger, 3, disabled=not s.plan.can_delete)
            self._add("Retour à l'aperçu", "preview", discord.ButtonStyle.secondary, 3)
            self._add("Annuler", "cancel", discord.ButtonStyle.secondary, 3)
        elif mode == DELETE_CONFIRM:
            self._add("Confirmer les suppressions", "delete_confirm", discord.ButtonStyle.danger, 3)
            self._add("Retour à la liste", "deletions", discord.ButtonStyle.secondary, 3)
            self._add("Annuler", "cancel", discord.ButtonStyle.secondary, 3)
        elif mode == DONE:
            if s.plan.can_delete:
                self._add("Examiner les suppressions", "deletions", discord.ButtonStyle.secondary, 3)
            if self.children:
                self._add("Fermer", "close", discord.ButtonStyle.secondary, 3)

    def _add(self, label: str, action: str, style: discord.ButtonStyle, row: int, *, disabled: bool = False) -> None:
        button = discord.ui.Button(label=label, style=style, row=row, disabled=disabled)
        button.callback = self._callback(action)
        self.add_item(button)

    def _callback(self, action: str):
        async def callback(interaction: discord.Interaction) -> None:
            await self.cog.handle_redesign_button(interaction, self, action)

        return callback

    def _select_callback(self, name: str, select: discord.ui.Select):
        async def callback(interaction: discord.Interaction) -> None:
            values = select.values or (interaction.data or {}).get("values") or []
            if values:
                await self.cog.handle_redesign_choice(interaction, self, name, values[0])

        return callback

    async def show(self, interaction: discord.Interaction) -> None:
        """Redessine le message de la refonte en réponse à un clic."""
        self.render()
        embed = self.current_embed()
        if self.session.mode in (DONE, CLOSED) and not self.children:
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
            await interaction.response.send_message("Une application est en cours : patientez jusqu'au rapport.", ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        if self.original is None or self.session.mode in (CLOSED, RUNNING):
            return
        text = (
            "Délai écoulé (10 minutes) : boutons désactivés. Les changements déjà appliqués restent ; relancez /refonte pour une nouvelle analyse."
            if self.session.applied else
            "Délai écoulé (10 minutes) : rien n'a été modifié. Relancez /refonte pour une nouvelle analyse."
        )
        try:
            await self.original.edit_original_response(content=text, embed=None, view=None)
        except discord.HTTPException:
            log.debug("Impossible de signaler l'expiration de /refonte", exc_info=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        log.error("Erreur dans l'interface de /refonte", exc_info=error)
        message = "Une erreur inattendue est survenue. Rien d'autre ne sera fait ; consultez /admin historique et les journaux du bot."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            pass
