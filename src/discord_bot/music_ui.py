"""Affichage de la musique : panneau du lecteur, boutons persistants, menus de choix, pages.

Les boutons du panneau sont des ``DynamicItem`` reconnus d'après leur identifiant
``samae:music:<serveur>:<session>:<entrée>:<action>``. Ces valeurs servent seulement à
retrouver le lecteur et le titre AFFICHÉS : à chaque clic, le cog revérifie que le serveur
est celui de l'interaction, que la session et le titre sont toujours en cours, et que la
personne a le droit d'agir (``music.permissions``).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import discord

from src.database.music import FavoriteRow, HistoryRow
from src.discord_bot.embeds import add_field, fit_embed, make_embed
from src.music.diagnostics import AudioReport, LavalinkInfo, StepStatus
from src.music.models import QueueEntry, SearchOutcome, Track
from src.music.player import LOOP_LABELS, LoopMode
from src.music.search import format_duration, source_label
from src.music.service import LiveVoiceState, Snapshot
from src.services import design
from src.services import text_limits as limits
from src.services.design import Tone

log = logging.getLogger(__name__)

BUTTON_TEMPLATE = r"samae:music:(?P<guild>[0-9]{1,20}):(?P<session>[0-9a-f]{8}):(?P<entry>[0-9]{1,18}):(?P<action>[a-z]{2,10})"
# action → (libellé, style, ligne)
BUTTONS = {
    "pause": ("⏸️ Pause", discord.ButtonStyle.secondary),
    "resume": ("▶️ Reprendre", discord.ButtonStyle.success),
    "skip": ("⏭️ Suivant", discord.ButtonStyle.secondary),
    "stop": ("⏹️ Arrêter", discord.ButtonStyle.danger),
    "loop": ("🔁 Répétition", discord.ButtonStyle.secondary),
    "queue": ("📜 File", discord.ButtonStyle.secondary),
    "favorite": ("⭐ Favori", discord.ButtonStyle.secondary),
}
PER_PAGE = 10
PROGRESS_SLOTS = 14
NO_MENTIONS = discord.AllowedMentions.none()
ICON = "🎵"


def track_line(track: Track, *, link: bool = True) -> str:
    title = design.md_link(track.title, track.uri) if link else design.escape_markdown(track.title)
    author = f" — {design.escape_markdown(track.author)}" if track.author else ""
    return f"{title}{author} `{format_duration(track.length_ms)}`"


def progress_bar(position_ms: int | None, length_ms: int) -> str:
    if position_ms is None or length_ms <= 0:
        return format_duration(length_ms) if length_ms > 0 else "durée inconnue"
    position_ms = min(max(position_ms, 0), length_ms)
    filled = round(PROGRESS_SLOTS * position_ms / length_ms)
    bar = "▬" * filled + "🔘" + "▬" * (PROGRESS_SLOTS - filled)
    return f"{bar} `{format_duration(position_ms)} / {format_duration(length_ms)}`"


def player_embed(snapshot: Snapshot) -> discord.Embed:
    """Panneau public du lecteur. La progression est celle du moment de l'affichage (pas d'actualisation en continu)."""
    entry = snapshot.current
    if entry is None:
        description = "Rien en cours. Ajoutez un titre avec `/play`."
        if not snapshot.voice_channel_id:
            description = "Le lecteur est arrêté : le bot a quitté le salon vocal."
        embed = make_embed(f"{ICON} Lecteur", description, tone=Tone.NEUTRAL, footer=design.footer("Musique"))
    else:
        track = entry.track
        state = "⏸️ En pause" if snapshot.paused else "▶️ En lecture"
        embed = make_embed(
            f"{ICON} {track.title}",
            f"{state}\n{progress_bar(snapshot.position_ms, track.length_ms)}",
            tone=Tone.INFO,
            url=track.uri,
            footer=design.footer("Musique", f"volume {snapshot.volume} %"
                                 + (f" · répétition : {LOOP_LABELS[snapshot.loop]}" if snapshot.loop is not LoopMode.OFF else "")),
        )
        if track.author:
            add_field(embed, "Artiste", design.escape_markdown(track.author), inline=True)
        add_field(embed, "Source", source_label(track.source), inline=True)
        add_field(embed, "Durée", format_duration(track.length_ms), inline=True)
        add_field(embed, "Demandé par", f"<@{entry.requester_id}>", inline=True)
        if snapshot.voice_channel_id:
            add_field(embed, "Salon", f"<#{snapshot.voice_channel_id}>", inline=True)
        if track.artwork_url:
            embed.set_thumbnail(url=track.artwork_url)
    if snapshot.upcoming:
        nxt = snapshot.upcoming[0].track
        add_field(
            embed,
            f"File : {len(snapshot.upcoming)} titre(s), {format_duration(snapshot.queue_length_ms)}",
            f"Suivant : {track_line(nxt)}",
        )
    elif entry is not None:
        add_field(embed, "File", "Aucun titre en attente.")
    if snapshot.last_problem:
        add_field(embed, "⚠️ Dernier problème", snapshot.last_problem)
    return fit_embed(embed)


class MusicButton(discord.ui.DynamicItem[discord.ui.Button], template=BUTTON_TEMPLATE):
    """Bouton persistant du panneau (fonctionne encore après un redémarrage, mais une session terminée est refusée)."""

    def __init__(self, guild_id: int, session_id: str, entry_id: int, action: str, *, disabled: bool = False) -> None:
        label, style = BUTTONS[action]
        super().__init__(
            discord.ui.Button(
                label=label,
                style=style,
                disabled=disabled,
                custom_id=f"samae:music:{guild_id}:{session_id}:{entry_id}:{action}",
            )
        )
        self.guild_id = guild_id
        self.session_id = session_id
        self.entry_id = entry_id
        self.action = action

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /):
        action = match["action"]
        if action not in BUTTONS:
            raise ValueError(action)  # identifiant inconnu : ignoré par discord.py
        return cls(int(match["guild"]), match["session"], int(match["entry"]), action)

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("MusicCog")
        try:
            if cog is None:
                await interaction.response.send_message("La musique est momentanément indisponible.", ephemeral=True)
                return
            await cog.handle_button(interaction, self.guild_id, self.session_id, self.entry_id, self.action)
        except Exception as exc:
            # Un clic doit toujours avoir une réponse : pas de « Échec de l'interaction » muet.
            log.error("Erreur sur le bouton de musique %s : %s", self.action, type(exc).__name__, exc_info=exc)
            text = "Cette action n'a pas abouti à cause d'une erreur inattendue. Réessayez ; si cela persiste, prévenez un administrateur."
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(text, ephemeral=True)
                else:
                    await interaction.response.send_message(text, ephemeral=True)
            except discord.HTTPException:
                log.warning("Impossible de signaler l'erreur du bouton de musique", exc_info=True)


def player_view(snapshot: Snapshot) -> discord.ui.View | None:
    """Boutons adaptés à l'état RÉEL du lecteur ; ``None`` quand la session est terminée (boutons retirés)."""
    if not snapshot.session_id:
        return None
    view = discord.ui.View(timeout=None)
    entry_id = snapshot.current.entry_id if snapshot.current else 0
    playing = snapshot.current is not None

    def add(action: str, *, disabled: bool = False) -> None:
        view.add_item(MusicButton(snapshot.guild_id, snapshot.session_id, entry_id, action, disabled=disabled))

    add("resume" if snapshot.paused else "pause", disabled=not playing)
    add("skip", disabled=not playing)
    add("stop", disabled=not playing and not snapshot.upcoming)
    add("loop")
    add("queue")
    add("favorite", disabled=not playing or not (snapshot.current and snapshot.current.track.uri))
    return view


# --- Pages éphémères (file, historique, favoris) ------------------------------------------


def _pages(title: str, lines: Sequence[str], *, empty: str, header: str | None = None) -> list[discord.Embed]:
    if not lines:
        return [make_embed(title, (f"{header}\n\n" if header else "") + f"*{empty}*", tone=Tone.NEUTRAL, footer=design.footer("Musique"))]
    pages = []
    chunks = [lines[i : i + PER_PAGE] for i in range(0, len(lines), PER_PAGE)]
    for number, chunk in enumerate(chunks, start=1):
        body = "\n".join(chunk)
        if header:
            body = f"{header}\n\n{body}"
        footer = design.footer("Musique", f"page {number}/{len(chunks)}")
        pages.append(fit_embed(make_embed(title, limits.truncate(body, limits.EMBED_DESCRIPTION), tone=Tone.INFO, footer=footer)))
    return pages


def queue_pages(snapshot: Snapshot) -> list[discord.Embed]:
    header = None
    if snapshot.current is not None:
        state = "⏸️ En pause" if snapshot.paused else "▶️ En cours"
        header = f"**{state}** : {track_line(snapshot.current.track)} — <@{snapshot.current.requester_id}>"
    lines = [f"`{i}.` {track_line(e.track)} — <@{e.requester_id}>" for i, e in enumerate(snapshot.upcoming, start=1)]
    return _pages(f"{ICON} File d'attente", lines, empty="Aucun titre en attente.", header=header)


def history_pages(rows: Sequence[HistoryRow], total: int) -> list[discord.Embed]:
    lines = []
    for row in rows:
        title = design.md_link(row.title, row.uri)
        author = f" — {design.escape_markdown(row.author)}" if row.author else ""
        lines.append(f"{design.relative_time(row.played_at)} · {title}{author} · <@{row.requested_by}>")
    header = f"{total} lecture(s) enregistrée(s) sur ce serveur." if total > len(rows) else None
    return _pages(f"{design.ICONS['history']} Historique musical", lines, empty="Aucune lecture enregistrée sur ce serveur.", header=header)


def favorites_pages(rows: Sequence[FavoriteRow]) -> list[discord.Embed]:
    lines = []
    for number, row in enumerate(rows, start=1):
        author = f" — {design.escape_markdown(row.author)}" if row.author else ""
        lines.append(f"`{number}.` {design.md_link(row.title, row.uri)}{author} `{format_duration(row.length_ms)}`")
    return _pages(
        "⭐ Vos favoris",
        lines,
        empty="Aucun favori. Pendant une lecture, utilisez le bouton ⭐ ou /favorite add.",
        header="Rejouer : menu ci-dessous. Retirer : `/favorite remove`." if rows else None,
    )


def enqueue_text(outcome: SearchOutcome | None, added: Sequence[QueueEntry], first_position: int, not_added: int) -> str:
    if len(added) == 1:
        where = "lancé maintenant" if first_position == 0 else f"position {first_position} dans la file"
        text = f"{track_line(added[0].track)} : {where}."
    else:
        name = design.escape_markdown(outcome.playlist_name) if outcome and outcome.playlist_name else "playlist"
        text = f"**{len(added)} titres** de « {name} » ajoutés à la file."
    notes = []
    if outcome is not None and outcome.truncated:
        notes.append(f"seuls les {len(outcome.tracks)} premiers titres de la playlist sont pris (limite du serveur)")
    if outcome is not None and outcome.rejected:
        notes.append(f"{outcome.rejected} titre(s) écarté(s) (trop long, direct ou illisible)")
    if not_added:
        notes.append(f"{not_added} titre(s) non ajouté(s) : la file est pleine")
    if notes:
        text += "\n" + "\n".join(f"• {note}" for note in notes)
    return text


# --- Choix d'un résultat (réservé à la personne qui a cherché) ----------------------------------

# Appelée avec l'interaction du choix et la valeur choisie (piste trouvée, ou lien d'un favori).
PickCallback = Callable[[discord.Interaction, Any], Awaitable[None]]


class TrackSelectView(discord.ui.View):
    """Menu de choix (résultats d'une recherche ou favoris à rejouer), avec expiration."""

    def __init__(
        self, author_id: int, tracks: Sequence[tuple[str, str, Any]], on_pick: PickCallback, *, placeholder: str, timeout: float = 60
    ) -> None:
        super().__init__(timeout=timeout)
        self._author_id = author_id
        self._on_pick = on_pick
        self._values = {str(index): value for index, (_, _, value) in enumerate(tracks)}
        self.original: discord.Interaction | None = None  # réponse de la commande qui porte le menu…
        self.message: discord.WebhookMessage | None = None  # … ou message de suivi qui le porte
        self.done = False
        options = [
            discord.SelectOption(label=limits.truncate(label, 100) or "?", description=limits.truncate(description, 100) or None, value=str(index))
            for index, (label, description, _) in enumerate(tracks[:25])
        ]
        select = discord.ui.Select(placeholder=placeholder, options=options, min_values=1, max_values=1)
        select.callback = self._picked
        self.select = select
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self._author_id:
            await interaction.response.send_message("Ce choix appartient à la personne qui a lancé la commande.", ephemeral=True)
            return False
        if self.done:
            try:
                await interaction.response.defer()
            except discord.HTTPException:
                pass
            return False
        return True

    async def _picked(self, interaction: discord.Interaction) -> None:
        self.done = True
        self.stop()
        value = self._values.get(self.select.values[0]) if self.select.values else None
        if value is None:
            await interaction.response.edit_message(content="Choix invalide : relancez la commande.", view=None, embed=None)
            return
        await interaction.response.edit_message(content="⏳ Ajout en cours…", view=None, embed=None)  # répond dans les 3 s
        await self._on_pick(interaction, value)

    async def on_timeout(self) -> None:
        if self.done or (self.original is None and self.message is None):
            return
        try:
            if self.message is not None:
                await self.message.edit(content="Choix expiré : relancez la commande.", view=None)
            else:
                await self.original.edit_original_response(content="Choix expiré : relancez la commande.", view=None, embed=None)
        except discord.HTTPException:
            log.debug("Impossible de signaler l'expiration d'un choix de musique", exc_info=True)


def result_options(tracks: Sequence[Track]) -> list[tuple[str, str, Track]]:
    return [
        (track.title, f"{track.author or 'artiste inconnu'} · {format_duration(track.length_ms)} · {source_label(track.source)}", track)
        for track in tracks
    ]


def results_embed(outcome: SearchOutcome, query: str) -> discord.Embed:
    lines = [f"`{i}.` {track_line(t)} · {source_label(t.source)}" for i, t in enumerate(outcome.tracks, start=1)]
    return make_embed(
        f"{ICON} Résultats pour « {design.escape_markdown(limits.truncate(query, 80))} »",
        "\n".join(lines) + "\n\nChoisissez un titre dans le menu (1 minute).",
        tone=Tone.INFO,
        footer=design.footer("Musique"),
    )


# --- Diagnostic du son (/audiocheck) ------------------------------------------------------------

STEP_ICONS = {StepStatus.OK: "✅", StepStatus.WAITING: "⏳", StepStatus.FAILED: "❌", StepStatus.UNKNOWN: "▫️"}


def audio_report_embed(
    report: AudioReport,
    *,
    lavalink_ready: bool,
    lavalink: LavalinkInfo | None,
    lavalink_problems: Sequence[str],
    live: LiveVoiceState | None,
    bot_voice: dict,
    frames: tuple[int, int, int] | None,
) -> discord.Embed:
    """Les cinq étapes du son, ce que Lavalink et Discord disent à l'instant, et la cause probable d'un silence."""
    lines = [f"{STEP_ICONS[step.status]} **{n}. {step.name}** : {design.escape_markdown(step.detail)}"
             for n, step in enumerate(report.steps, start=1)]
    problems = list(lavalink_problems)
    if not lavalink_ready:
        problems.insert(0, "Lavalink n'est pas joignable : vérifiez qu'il tourne et que LAVALINK_PASSWORD est le bon (/etat).")
    if bot_voice.get("server_mute"):
        problems.append("Le bot est rendu muet par le serveur : personne ne l'entend (à retirer dans le salon vocal).")
    if bot_voice.get("suppress"):
        problems.append("Dans une scène, le bot est spectateur : un modérateur doit l'inviter à parler.")
    if live is not None and live.exists and not live.channel_sent:
        problems.append("Lavalink n'a pas reçu l'identifiant du salon vocal (exigé par DAVE) : vérifiez wavelink 3.5.2.")
    if report.problem:
        problems.append(report.problem)
    tone = Tone.FAILURE if problems else (Tone.SUCCESS if report.all_ok else Tone.INFO)
    embed = make_embed("🔊 Diagnostic du son", "\n".join(lines), tone=tone, footer=design.footer("Musique", "/audiocheck"))
    if lavalink is not None:
        dave = {True: "oui", False: "**non**", None: "inconnu"}[lavalink.supports_dave]
        add_field(embed, "Lavalink", f"version {design.escape_markdown(lavalink.version)} · DAVE : {dave}\n"
                  f"sources : {design.escape_markdown(', '.join(lavalink.source_managers) or 'aucune')}", inline=False)
    if live is not None:
        if not live.exists:
            add_field(embed, "Lecteur Lavalink (maintenant)", "aucun lecteur pour ce serveur chez Lavalink", inline=False)
        else:
            state = "connecté" if live.connected else "**non connecté**"
            ping = f", ping {live.ping_ms} ms" if live.ping_ms >= 0 else ""
            add_field(embed, "Lecteur Lavalink (maintenant)",
                      f"voix : {state}{ping} · titre : {'oui' if live.has_track else 'non'} · position {format_duration(live.position_ms)}"
                      f" · volume {live.volume} · pause : {'oui' if live.paused else 'non'}\n"
                      f"session vocale transmise : {'oui' if live.voice_sent else '**non**'} · salon transmis : "
                      f"{'oui' if live.channel_sent else '**non**'}", inline=False)
    if bot_voice.get("channel_id"):
        add_field(embed, "Bot dans Discord", f"<#{bot_voice['channel_id']}> · muet (serveur) : {'**oui**' if bot_voice.get('server_mute') else 'non'}")
    if frames is not None:
        sent, nulled, deficit = frames
        add_field(embed, "Trames audio (Lavalink, dernière minute)", f"envoyées {sent} · vides {nulled} · manquantes {deficit}")
    if problems:
        add_field(embed, "⚠️ Cause probable", "\n".join(f"• {design.escape_markdown(p)}" for p in problems[:4]), inline=False)
    elif report.all_ok:
        add_field(embed, "Résultat", "Le son part bien vers Discord. Si personne n'entend : vérifiez le volume du bot dans "
                  "Discord (clic droit sur le bot > Volume de l'utilisateur) et que votre application Discord est à jour.", inline=False)
    return fit_embed(embed)
