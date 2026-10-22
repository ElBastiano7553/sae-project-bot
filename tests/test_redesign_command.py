"""Commande /refonte de bout en bout, sur un faux serveur Discord en mémoire (aucun réseau, aucun vrai serveur)."""

from __future__ import annotations

import asyncio
import itertools
import json
import sys
from types import SimpleNamespace

import discord
import pytest

from src.config import Settings
from src.database import Database
from src.discord_bot.bot import EXTENSIONS, SaeBot
from src.discord_bot.redesign import RedesignExecutor
from src.discord_bot.redesign_ui import ANALYSIS, CLOSED, CONFIRM, DONE, PREVIEW
from src.services.meetings import Actor
from src.services.server_redesign import OpKind, RedesignOptions, ResultStatus, build_plan
from src.services.server_structure import GuildCategory, GuildChannel, GuildRole, GuildSnapshot

GUILD_ID = 42
ADMIN_ID, MEMBER_ID = 111111111111111111, 222222222222222222
_ids = itertools.count(900000)


# --- Faux Discord -----------------------------------------------------------------------------


class _Response:
    status = 403
    reason = "Forbidden"


class FakeChannel:
    def __init__(self, guild, channel_id, name, kind, category_id=None, *, overwrites=None) -> None:
        self.guild, self.id, self.name, self.kind, self.category_id = guild, channel_id, name, kind, category_id
        self.position = 0
        self.overwrites = overwrites or {}
        self.fail = None  # exception levée à la prochaine modification
        self.delay = 0.0

    async def edit(self, *, reason=None, **changes):
        self.guild.calls.append(("edit", self.id, dict(changes)))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail is not None:
            raise self.fail
        assert changes.get("sync_permissions", False) is False, "un déplacement ne doit jamais changer les permissions"
        if "name" in changes:
            self.name = changes["name"]
        if "category" in changes:
            self.category_id = changes["category"].id

    async def move(self, *, after, reason=None):
        self.guild.calls.append(("move", self.id, after.id))

    def permissions_for(self, member) -> discord.Permissions:
        return discord.Permissions.all()  # diagnostic des destinations après la refonte

    async def delete(self, **kwargs):  # pragma: no cover - ne doit jamais être appelé
        raise AssertionError("la refonte ne supprime jamais rien")


class FakeRole:
    def __init__(self, role_id, name, position=1, *, default=False, managed=False) -> None:
        self.id, self.name, self.position, self.managed, self._default = role_id, name, position, managed, default
        self.permissions = discord.Permissions.none()

    def is_default(self):
        return self._default


class FakeBotMember:
    """Le bot dans le serveur : sert de clé dans les permissions des catégories privées."""

    def __init__(self, member_id: int, top_role) -> None:
        self.id = member_id
        self.top_role = top_role

    def __hash__(self) -> int:
        return hash(self.id)


class FakeGuild:
    def __init__(self) -> None:
        self.id = GUILD_ID
        self.calls: list = []
        self.categories: dict[int, FakeChannel] = {}
        self.text_and_voice: dict[int, FakeChannel] = {}
        self.default_role = FakeRole(GUILD_ID, "@everyone", 0, default=True)
        self.roles = [self.default_role, FakeRole(2, "Samae", 5, managed=True)]
        self.me = FakeBotMember(999, self.roles[1])
        self.bot_permissions = {"manage_channels", "view_channel"}
        self.fail_create = None

    def add_category(self, name):
        cat = FakeChannel(self, next(_ids), name, "category")
        self.categories[cat.id] = cat
        return cat

    def add_channel(self, name, category=None, kind="text"):
        ch = FakeChannel(self, next(_ids), name, kind, category.id if category else None)
        self.text_and_voice[ch.id] = ch
        return ch

    def get_channel(self, channel_id):
        return self.categories.get(channel_id) or self.text_and_voice.get(channel_id)

    def get_role(self, role_id):
        return next((r for r in self.roles if r.id == role_id), None)

    async def create_category(self, name, *, overwrites=None, reason=None):
        self.calls.append(("create_category", name, overwrites))
        if self.fail_create:
            raise self.fail_create
        cat = self.add_category(name)
        cat.overwrites = overwrites or {}
        return cat

    async def _create(self, name, kind, category, overwrites, reason):
        self.calls.append((f"create_{kind}", name, category.id, overwrites))
        ch = self.add_channel(name, category, kind)
        ch.overwrites = overwrites or {}
        return ch

    async def create_text_channel(self, name, *, category, overwrites=None, reason=None):
        return await self._create(name, "text", category, overwrites, reason)

    async def create_voice_channel(self, name, *, category, overwrites=None, reason=None):
        return await self._create(name, "voice", category, overwrites, reason)

    async def create_role(self, *, name, permissions, mentionable, hoist, reason=None):
        assert permissions.value == 0 and not mentionable and not hoist
        self.calls.append(("create_role", name))
        role = FakeRole(next(_ids), name)
        self.roles.append(role)
        return role

    def snapshot(self) -> GuildSnapshot:
        """Équivalent de discord_bot.snapshot.snapshot_guild pour ce faux serveur."""
        return GuildSnapshot(
            categories=tuple(
                GuildCategory(c.id, c.name, c.position, everyone_can_view=self.default_role not in c.overwrites)
                for c in self.categories.values()
            ),
            channels=tuple(GuildChannel(c.id, c.name, c.kind, c.category_id) for c in self.text_and_voice.values()),
            roles=tuple(GuildRole(r.id, r.name, r.position, managed=r.managed, is_default=r.is_default()) for r in self.roles),
            bot_permissions=frozenset(self.bot_permissions),
            bot_top_role_position=5,
        )

    def names(self) -> dict[str, str | None]:
        return {c.name: (self.categories[c.category_id].name if c.category_id else None) for c in self.text_and_voice.values()}


def old_guild() -> FakeGuild:
    guild = FakeGuild()
    layout = {
        "01 · ACCUEIL & ORGANISATION": ["bienvenue", "annonces", "planning-et-deadlines", "discussion-generale", "decisions-et-comptes-rendus"],
        "02 · CONCEPTION DU JEU": ["idees-et-game-design", "gameplay-et-ia", "assets-et-audio"],
        "03 · DÉVELOPPEMENT": ["developpement-python", "reseau-multijoueur", "bugs-et-tests", "code-review"],
        "04 · GITLAB AUTOMATISÉ": ["gitlab-activite", "builds-et-tests", "bilan-ia"],
        "05 · LIVRABLES SAE": ["documentation-et-preuves", "preparation-revues"],
    }
    for cat_name, channels in layout.items():
        cat = guild.add_category(cat_name)
        for name in channels:
            guild.add_channel(name, cat)
    vocal = guild.add_category("06 · VOCAL")
    guild.add_channel("reunion-equipe", vocal, "voice")
    guild.add_channel("travail-en-commun", vocal, "voice")
    guild.add_channel("hors-sujet")  # salon hors de la structure cible : jamais touché
    return guild


class FakeMember(discord.Member):
    """Vrai type Member (isinstance), avec des permissions choisies par le test."""

    def __init__(self, user_id: int, **perms) -> None:
        self._user = SimpleNamespace(id=user_id, name=f"user{user_id}", display_name=f"user{user_id}", bot=False)
        self._fake_perms = discord.Permissions(**perms)

    @property
    def guild_permissions(self):
        return self._fake_perms

    @property
    def roles(self):
        return []

    @property
    def display_name(self):
        return self._user.display_name

    def __str__(self):
        return self._user.name


class FakeResponse:
    def __init__(self) -> None:
        self.sent, self.edited, self._done = [], [], False

    def is_done(self):
        return self._done

    async def send_message(self, content=None, **kwargs):
        self.sent.append({"content": content, **kwargs})
        self._done = True

    async def edit_message(self, **kwargs):
        self.edited.append(kwargs)
        self._done = True

    async def defer(self, **kwargs):
        self._done = True


class FakeFollowup:
    def __init__(self) -> None:
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append({"content": content, **kwargs})


class FakeInteraction:
    def __init__(self, client, guild, user) -> None:
        self.client, self.guild, self.user = client, guild, user
        self.response, self.followup = FakeResponse(), FakeFollowup()
        self.edits: list[dict] = []
        self.command = None

    async def edit_original_response(self, **kwargs):
        self.edits.append(kwargs)

    def last_embed(self):
        for payload in reversed(self.response.edited + self.edits):  # la réponse précède les modifications
            if payload.get("embed") is not None:
                return payload["embed"]
        return None


def text(embed) -> str:
    return " ".join([embed.title or "", embed.description or "", *[f"{f.name} {f.value}" for f in embed.fields]])


ADMIN = FakeMember(ADMIN_ID, manage_guild=True, manage_channels=True)


def patch_snapshot(bot, monkeypatch) -> None:
    """discord.py charge chaque extension dans un nouvel objet module : on patche celui du cog chargé."""
    module = sys.modules[type(bot.get_cog("RedesignCog")).__module__]
    monkeypatch.setattr(module, "snapshot_guild", lambda g: g.snapshot())


@pytest.fixture
async def env(db, monkeypatch):
    bot = SaeBot(Settings.from_env({"DISCORD_GUILD_ID": str(GUILD_ID)}, require_token=False), db)
    for extension in EXTENSIONS:
        await bot.load_extension(extension)
    guild = old_guild()
    patch_snapshot(bot, monkeypatch)
    monkeypatch.setattr(SaeBot, "target_guild", property(lambda self: guild))
    yield bot, bot.get_cog("RedesignCog"), guild
    await bot.close()


async def open_refonte(bot, cog, guild, user=ADMIN, **options):
    interaction = FakeInteraction(bot, guild, user)
    await cog.refonte.callback(cog, interaction, **options)
    return interaction


def view_of(interaction):
    return interaction.followup.sent[0]["view"]


async def click(bot, cog, guild, view, action, user=ADMIN):
    interaction = FakeInteraction(bot, guild, user)
    if not await view.interaction_check(interaction):
        return interaction
    await cog.handle_redesign_button(interaction, view, action)
    return interaction


# --- Analyse et aperçu ----------------------------------------------------------------------


async def test_analysis_and_preview_have_no_side_effect(env):
    bot, cog, guild = env
    before = guild.names()
    interaction = await open_refonte(bot, cog, guild)
    sent = interaction.followup.sent[0]
    assert sent["ephemeral"] and "rien n'a été modifié" in sent["embed"].description
    view = view_of(interaction)
    await click(bot, cog, guild, view, "preview")
    assert view.session.mode == PREVIEW and len(view.pages()) >= 2
    await click(bot, cog, guild, view, "next")
    await click(bot, cog, guild, view, "analyse")
    assert guild.calls == [] and guild.names() == before
    assert bot.bindings_repo.get_all() == {} and bot.admin_log.recent() == []


async def test_preview_pages_respect_discord_limits(env):
    bot, cog, guild = env
    view = view_of(await open_refonte(bot, cog, guild, facultatifs=True, roles=False))
    await click(bot, cog, guild, view, "preview")
    for page in view.pages():
        assert len(page) <= 6000 and len(page.description) <= 4096
    labels = [item.label for item in view.children]
    assert labels[:3] == ["Précédent", f"1/{len(view.pages())}", "Suivant"]
    assert "Appliquer la refonte" in labels and "Annuler" in labels


# --- Contrôles d'accès ------------------------------------------------------------------------


async def test_member_without_admin_rights_is_refused(env):
    bot, cog, guild = env
    interaction = await open_refonte(bot, cog, guild, user=FakeMember(MEMBER_ID))
    assert "réservée aux administrateurs" in interaction.response.sent[0]["embed"].description
    assert interaction.followup.sent == []


async def test_admin_without_manage_channels_is_refused(env):
    bot, cog, guild = env
    interaction = await open_refonte(bot, cog, guild, user=FakeMember(ADMIN_ID, manage_guild=True))
    assert "Gérer les salons" in interaction.response.sent[0]["embed"].description
    roles = await open_refonte(bot, cog, guild, user=ADMIN, roles=True)
    assert "Gérer les rôles" in roles.response.sent[0]["embed"].description


async def test_another_user_cannot_click_the_buttons(env):
    bot, cog, guild = env
    view = view_of(await open_refonte(bot, cog, guild))
    other = await click(bot, cog, guild, view, "confirm", user=FakeMember(MEMBER_ID, administrator=True))
    assert "Seule la personne" in other.response.sent[0]["content"]
    assert guild.calls == []


async def test_rights_are_checked_again_at_confirmation(env):
    bot, cog, guild = env
    view = view_of(await open_refonte(bot, cog, guild))
    await click(bot, cog, guild, view, "apply")
    demoted = FakeMember(ADMIN_ID, manage_guild=True)  # même personne, « Gérer les salons » retiré entre-temps
    interaction = await click(bot, cog, guild, view, "confirm", user=demoted)
    assert "Gérer les salons" in interaction.response.sent[0]["embed"].description
    assert guild.calls == []


async def test_missing_bot_permission_blocks_the_apply_button_and_the_server(env):
    bot, cog, guild = env
    guild.bot_permissions = {"view_channel"}
    view = view_of(await open_refonte(bot, cog, guild))
    apply = next(item for item in view.children if item.label == "Appliquer la refonte")
    assert apply.disabled
    assert "Gérer les salons" in text(view.current_embed())
    interaction = await click(bot, cog, guild, view, "apply")  # clic forcé : refusé côté serveur
    assert "ne peut pas être appliqué" in interaction.response.sent[0]["embed"].description
    view.session.mode = CONFIRM
    await click(bot, cog, guild, view, "confirm")
    assert guild.calls == []


# --- Application ---------------------------------------------------------------------------------


async def apply_refonte(bot, cog, guild, **options):
    view = view_of(await open_refonte(bot, cog, guild, **options))
    await click(bot, cog, guild, view, "apply")
    assert view.session.mode == CONFIRM
    interaction = await click(bot, cog, guild, view, "confirm")
    return view, interaction


async def test_confirmed_refonte_applies_the_plan_and_reports_faithfully(env):
    bot, cog, guild = env
    view, interaction = await apply_refonte(bot, cog, guild)
    assert view.session.mode == DONE
    names = guild.names()
    assert names["décisions"] == "CONCEPTION DU JEU"
    assert names["gitlab-et-commits"] == "DÉVELOPPEMENT"
    assert names["comptes-rendus"] == "ORGANISATION" and names["réunions"] == "ORGANISATION"
    assert names["bot-logs"] == "ADMINISTRATION"
    assert names["hors-sujet"] is None  # salon hors cible : intact
    assert "04 · GITLAB AUTOMATISÉ" in {c.name for c in guild.categories.values()}  # catégorie vidée, jamais supprimée
    outcome = view.session.outcome
    assert outcome.complete and not outcome.with_status(ResultStatus.FAILED)
    embed = interaction.last_embed()
    assert embed.title == "Refonte appliquée"
    assert any(op.target == "04 · GITLAB AUTOMATISÉ" for op in outcome.manual)
    assert bot.admin_log.recent()[0].action == "refonte"


async def test_private_category_hides_from_everyone(env):
    bot, cog, guild = env
    await apply_refonte(bot, cog, guild)
    admin_cat = next(c for c in guild.categories.values() if c.name == "ADMINISTRATION")
    assert admin_cat.overwrites[guild.default_role].view_channel is False
    assert admin_cat.overwrites[guild.me].view_channel is True
    logs = next(c for c in guild.text_and_voice.values() if c.name == "bot-logs")
    assert logs.overwrites[guild.default_role].view_channel is False


async def test_configuration_is_saved_by_identifier(env):
    bot, cog, guild = env
    await apply_refonte(bot, cog, guild)
    by_name = {c.name: c.id for c in guild.text_and_voice.values()}
    bindings = bot.bindings_repo.get_all()
    assert bindings["comptes_rendus"] == by_name["comptes-rendus"]
    assert bindings["decisions"] == by_name["décisions"]
    assert bindings["gitlab_activite"] == by_name["gitlab-et-commits"]
    assert set(bot.category_bindings) == {"accueil", "commun", "conception", "organisation", "developpement", "livrables", "vocal", "administration"}


async def test_second_run_is_idempotent(env):
    bot, cog, guild = env
    await apply_refonte(bot, cog, guild)
    channels, categories = len(guild.text_and_voice), len(guild.categories)
    guild.calls.clear()
    view = view_of(await open_refonte(bot, cog, guild))
    plan = view.session.plan
    assert plan.actions == [] and not plan.can_apply
    assert "Aucune modification nécessaire" in text(view.current_embed())
    assert (len(guild.text_and_voice), len(guild.categories)) == (channels, categories) and guild.calls == []


async def test_changes_since_preview_invalidate_the_plan(env):
    bot, cog, guild = env
    view = view_of(await open_refonte(bot, cog, guild))
    await click(bot, cog, guild, view, "apply")
    fingerprint = view.session.plan.fingerprint
    next(c for c in guild.text_and_voice.values() if c.name == "bilan-ia").name = "bilan-ia-v2"  # changement concurrent
    interaction = await click(bot, cog, guild, view, "confirm")
    assert view.session.mode == PREVIEW and view.session.plan.fingerprint != fingerprint
    assert "Le serveur a changé" in interaction.last_embed().description
    assert guild.calls == [] and bot.bindings_repo.get_all() == {}


async def test_partial_failure_is_reported_and_other_operations_continue(env):
    bot, cog, guild = env
    target = next(c for c in guild.text_and_voice.values() if c.name == "bugs-et-tests")
    target.fail = discord.Forbidden(_Response(), "Missing Permissions")
    view, interaction = await apply_refonte(bot, cog, guild)
    outcome = view.session.outcome
    failed = outcome.with_status(ResultStatus.FAILED)
    assert [r.operation.key for r in failed] == ["bugs"]
    assert "permission refusée" in failed[0].detail
    assert not outcome.complete
    assert interaction.last_embed().title == "Refonte partiellement appliquée"
    assert "n'est **pas** terminée" in interaction.last_embed().description
    assert "décisions" in guild.names()  # les autres opérations ont été faites


async def test_failed_category_creation_skips_dependent_channels(env):
    bot, cog, guild = env
    guild.fail_create = discord.HTTPException(SimpleNamespace(status=500, reason="Server Error"), "boom")
    view, _ = await apply_refonte(bot, cog, guild)
    results = {(r.operation.kind, r.operation.key): r for r in view.session.outcome.results}
    category = results[(OpKind.CREATE_CATEGORY, "organisation")]
    assert category.status is ResultStatus.FAILED and "500" in category.detail
    assert results[(OpKind.CREATE_CHANNEL, "reunions")].status is ResultStatus.SKIPPED
    assert results[(OpKind.MOVE_CHANNEL, "planning")].status is ResultStatus.SKIPPED  # déplacement vers ORGANISATION impossible
    # Le salon existant reste associé par son identifiant, à son ancienne place.
    assert results[(OpKind.BIND, "planning")].status is ResultStatus.DONE
    assert "reunions" not in bot.bindings_repo.get_all()


async def test_timeout_is_reported_as_uncertain(db):
    guild = old_guild()
    plan = build_plan(_layout(), guild.snapshot())
    slow = next(c for c in guild.text_and_voice.values() if c.name == "bugs-et-tests")
    slow.delay = 1
    results = await RedesignExecutor(guild, actor="alice", timeout=0.05).apply(plan)
    failed = [r for r in results if r.status is ResultStatus.FAILED]
    assert [r.operation.key for r in failed] == ["bugs"] and "délai dépassé" in failed[0].detail


def _layout():
    from src.config import load_server_layout

    return load_server_layout()


async def test_roles_are_created_without_any_permission(env):
    bot, cog, guild = env
    guild.bot_permissions.add("manage_roles")
    member = FakeMember(ADMIN_ID, manage_guild=True, manage_channels=True, manage_roles=True)
    view = view_of(await open_refonte(bot, cog, guild, user=member, roles=True))
    await click(bot, cog, guild, view, "apply", user=member)
    await click(bot, cog, guild, view, "confirm", user=member)
    created = [call[1] for call in guild.calls if call[0] == "create_role"]
    assert created == ["Responsable projet", "Game Design", "Développement", "Art & Design", "Testeur"]


async def test_cancel_changes_nothing(env):
    bot, cog, guild = env
    view = view_of(await open_refonte(bot, cog, guild))
    interaction = await click(bot, cog, guild, view, "cancel")
    assert view.session.mode == CLOSED and "rien n'a été modifié" in interaction.response.edited[0]["content"]
    assert guild.calls == []


async def test_expired_view_says_nothing_was_changed(env):
    bot, cog, guild = env
    opening = await open_refonte(bot, cog, guild)
    view = view_of(opening)
    await view.on_timeout()
    assert "rien n'a été modifié" in opening.edits[-1]["content"]


# --- Données préservées : réunions, sauvegarde, journal -----------------------------------------------


async def test_meetings_survive_and_reports_go_to_the_new_minutes_channel(env):
    bot, cog, guild = env
    planning = next(c for c in guild.text_and_voice.values() if c.name == "planning-et-deadlines")
    leader = Actor(ADMIN_ID, "Alice")
    meeting = bot.meetings.create(leader, title="Point sprint", steps=["Bilan"], channel_id=planning.id).meeting
    bot.meetings.set_panel(meeting.id, planning.id, 123456)
    before = bot.meetings.get(meeting.id)

    await apply_refonte(bot, cog, guild)

    after = bot.meetings.get(meeting.id)
    assert (after.meeting.channel_id, after.meeting.panel_message_id) == (planning.id, 123456)
    assert after.meeting.revision == before.meeting.revision and len(bot.meetings.repo.list()) == 1
    assert planning.name == "planning"  # renommé : même identifiant, panneau toujours valide
    from src.discord_bot.cogs.meetings import REPORT_CHANNEL_KEYS

    bindings = bot.bindings_repo.get_all()
    assert bindings[REPORT_CHANNEL_KEYS[0]] == next(c.id for c in guild.text_and_voice.values() if c.name == "comptes-rendus")


async def test_backup_and_journal_are_written_before_and_after(tmp_path, monkeypatch):
    database = Database(tmp_path / "bot.sqlite3")
    settings = Settings.from_env(
        {"DISCORD_GUILD_ID": str(GUILD_ID), "DATABASE_PATH": str(tmp_path / "bot.sqlite3"), "BACKUP_INTERVAL_HOURS": "0"},
        require_token=False,
    )
    bot = SaeBot(settings, database)
    try:
        for extension in EXTENSIONS:
            await bot.load_extension(extension)
        guild = old_guild()
        patch_snapshot(bot, monkeypatch)
        monkeypatch.setattr(SaeBot, "target_guild", property(lambda self: guild))
        view, _ = await apply_refonte(bot, bot.get_cog("RedesignCog"), guild)
        outcome = view.session.outcome
        assert outcome.backup and "avantrefonte" in outcome.backup
        assert (tmp_path / "backups" / outcome.backup).exists()
        journal = json.loads((tmp_path / "refonte" / outcome.journal).read_text(encoding="utf-8"))
        assert journal["plan"] == view.session.plan.fingerprint
        assert any(r["operation"]["current"] == "decisions-et-comptes-rendus" for r in journal["results"])  # anciens noms gardés
        assert "token" not in json.dumps(journal).lower()
    finally:
        await bot.close()
        database.close()


async def test_refused_backup_means_nothing_is_applied(env, monkeypatch):
    bot, cog, guild = env
    from src.services.backup import BackupError

    monkeypatch.setattr(type(bot.database), "in_memory", False, raising=False)
    bot.database.in_memory = False

    def broken(reason=""):
        raise BackupError("disque plein")

    monkeypatch.setattr(bot, "run_backup", broken)
    view, _ = await apply_refonte(bot, cog, guild)
    assert all(r.status is ResultStatus.SKIPPED for r in view.session.outcome.results)
    assert guild.calls == [] and bot.bindings_repo.get_all() == {}
    assert "sauvegarde impossible" in bot.admin_log.recent()[0].outcome
