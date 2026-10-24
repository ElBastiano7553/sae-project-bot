"""/projet ajouter : adresse GitHub ou GitLab, doublons, récapitulatif puis confirmation (administrateurs)."""

from __future__ import annotations

import pytest

from src.services.projects import ProjectError, parse_repository_url
from tests.fake_guild import FakeGuild
from tests.test_projects import (
    GITHUB_SECRET,
    GITLAB_SECRET,
    GUILD,
    OTHER_GUILD,
    Interaction,
    Member,
    embed_text,
    make_bot,  # noqa: F401  (fixture)
)

# --- Lecture de l'adresse (sans réseau) -------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "provider", "external_id", "instance", "name"),
    [
        ("https://github.com/Equipe/Jeu", "github", "equipe/jeu", "", "Jeu"),
        ("https://github.com/equipe/jeu.git", "github", "equipe/jeu", "", "jeu"),
        ("https://www.github.com/equipe/jeu/tree/main/src", "github", "equipe/jeu", "", "jeu"),
        ("<https://github.com/equipe/jeu/>", "github", "equipe/jeu", "", "jeu"),
        ("git@github.com:equipe/jeu.git", "github", "equipe/jeu", "", "jeu"),
        ("https://gitlab.com/groupe/projet", "gitlab", "groupe/projet", "https://gitlab.com", "projet"),
        ("https://gitlab.com/groupe/sous-groupe/Projet.git", "gitlab", "groupe/sous-groupe/projet", "https://gitlab.com", "Projet"),
        ("https://gitlab.com/groupe/projet/-/merge_requests/3", "gitlab", "groupe/projet", "https://gitlab.com", "projet"),
        ("https://gitlab.cri.epita.fr/promo/sae/jeu", "gitlab", "promo/sae/jeu", "https://gitlab.cri.epita.fr", "jeu"),
        ("git@gitlab.com:groupe/projet.git", "gitlab", "groupe/projet", "https://gitlab.com", "projet"),
    ],
)
def test_supported_addresses(url, provider, external_id, instance, name):
    parsed = parse_repository_url(url)
    assert (parsed.provider, parsed.external_id, parsed.instance_url, parsed.default_name) == (provider, external_id, instance, name)


def test_self_hosted_gitlab_is_recognized_from_configuration():
    with pytest.raises(ProjectError, match="non reconnu"):
        parse_repository_url("https://code.ecole.fr/a/b")
    assert parse_repository_url("https://code.ecole.fr/a/b", gitlab_hosts=("code.ecole.fr",)).provider == "gitlab"


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("", "vide"),
        ("github.com/equipe/jeu", "https://"),
        ("http://github.com/equipe/jeu", "https://"),
        ("https://jean:motdepasse@github.com/equipe/jeu", "mot de passe"),
        ("https://ghp_abc123@github.com/equipe/jeu", "identifiant"),
        ("https://github.com/equipe", "incomplète"),
        ("https://gitlab.com/projet", "incomplète"),
        ("https://bitbucket.org/equipe/jeu", "non reconnu"),
        ("https://github.com/-equipe/jeu", "propriétaire/depot"),
        ("https://gitlab.com/groupe/pro jet", "groupe/projet"),
        ("https://github.com/" + "a" * 300, "trop longue"),
    ],
)
def test_invalid_addresses_have_understandable_messages(url, message):
    with pytest.raises(ProjectError, match=message):
        parse_repository_url(url)


# --- Commande ---------------------------------------------------------------------------------


class ConfirmClick(Interaction):
    def __init__(self, client, user) -> None:
        super().__init__(client, None, user)
        self.edits: list[dict] = []

        class _Response:
            def __init__(self) -> None:
                self.sent, self.edited = [], []

            def is_done(self) -> bool:
                return False

            async def send_message(self, content=None, **kwargs):
                self.sent.append({"content": content, **kwargs})

            async def edit_message(self, **kwargs):
                self.edited.append(kwargs)

        self.response = _Response()

    async def edit_original_response(self, **kwargs):
        self.edits.append(kwargs)


async def add(bot, guild=None, user=None, **options) -> dict:
    cog = bot.get_cog("ProjectsCog")
    interaction = Interaction(bot, guild or FakeGuild(), user or Member(1, admin=True))
    interaction.guild.name = "SAE · GAME DEV"
    await cog.add.callback(cog, interaction, **options)
    return interaction.response.sent[0]


async def confirm(bot, sent, user=None) -> ConfirmClick:
    view = sent["view"]
    click = ConfirmClick(bot, user or Member(1, admin=True))
    if await view.interaction_check(click):
        await view.confirm.callback(click)
    return click


async def test_preview_shows_provider_name_and_server_and_saves_nothing(make_bot):  # noqa: F811
    bot = await make_bot()
    sent = await add(bot, url="https://github.com/Equipe/Jeu")
    text = embed_text(sent["embed"])
    assert sent["ephemeral"] and "GitHub" in text and "Jeu" in text and "SAE · GAME DEV" in text
    assert "Surveillance : pas encore" in text and "Aucun token" in text
    assert bot.projects_repo.list_for_guild(GUILD) == []


async def test_confirmation_registers_the_project_as_not_yet_connected(make_bot):  # noqa: F811
    bot = await make_bot()
    sent = await add(bot, url="https://gitlab.com/groupe/projet", nom="Outils de l'équipe")
    click = await confirm(bot, sent)
    [record] = bot.projects_repo.list_for_guild(GUILD)
    assert (record.provider, record.external_id, record.instance_url, record.name, record.created_by) == (
        "gitlab", "groupe/projet", "https://gitlab.com", "Outils de l'équipe", 1
    )
    result = embed_text(click.edits[-1]["embed"])
    assert "pas encore connecté" in result and f"/projet modifier projet:{record.id}" in result
    assert bot.projects_repo.routes(GUILD, record.id) == ()  # ajout et salons restent séparés
    [action] = bot.admin_log.recent()
    assert action.action == "projet ajouter" and "groupe/projet" in action.outcome


async def test_cancel_or_another_member_changes_nothing(make_bot):  # noqa: F811
    bot = await make_bot()
    sent = await add(bot, url="https://github.com/equipe/jeu")
    intruder = await confirm(bot, sent, user=Member(2, admin=True))
    assert intruder.response.sent and "Seule la personne" in intruder.response.sent[0]["content"]
    view = sent["view"]
    await view.cancel.callback(ConfirmClick(bot, Member(1, admin=True)))
    assert bot.projects_repo.list_for_guild(GUILD) == [] and bot.admin_log.recent() == []


async def test_invalid_address_is_explained(make_bot):  # noqa: F811
    bot = await make_bot()
    sent = await add(bot, url="https://jean:secret@github.com/equipe/jeu")
    text = embed_text(sent["embed"])
    assert "Adresse refusée" in sent["embed"].title and "jamais d'identifiants" in text
    assert "jean:secret" not in text  # l'adresse fautive n'est pas répétée
    assert "view" not in sent


async def test_duplicate_in_the_same_server_is_refused_even_with_another_case(make_bot):  # noqa: F811
    bot = await make_bot()
    bot.projects_repo.create(GUILD, "github", "equipe/jeu", "Jeu", created_by=1)
    sent = await add(bot, url="https://github.com/EQUIPE/JEU.git")
    assert "déjà suivi" in embed_text(sent["embed"]) and "view" not in sent


async def test_same_repository_is_allowed_on_another_server_but_not_twice_here(make_bot):  # noqa: F811
    bot = await make_bot()
    bot.projects_repo.create(OTHER_GUILD, "github", "equipe/jeu", "Leur jeu", created_by=9)
    sent = await add(bot, url="https://github.com/equipe/jeu")
    assert "view" in sent  # l'autre serveur ne compte pas
    await confirm(bot, sent)
    assert len(bot.projects_repo.list_for_guild(GUILD)) == 1 and len(bot.projects_repo.list_for_guild(OTHER_GUILD)) == 1


async def test_duplicate_detected_again_at_confirmation(make_bot):  # noqa: F811
    bot = await make_bot()
    sent = await add(bot, url="https://github.com/equipe/jeu")
    bot.projects_repo.create(GUILD, "github", "equipe/jeu", "Ajouté entre-temps", created_by=3)
    click = await confirm(bot, sent)
    assert "déjà suivi" in click.edits[-1]["content"]
    assert len(bot.projects_repo.list_for_guild(GUILD)) == 1


async def test_repository_already_followed_by_env_is_refused(make_bot):  # noqa: F811
    bot = await make_bot(GITHUB_WEBHOOK_SECRET=GITHUB_SECRET)
    sent = await add(bot, url="https://github.com/elbastiano7553/Game-Project")
    text = embed_text(sent["embed"])
    assert ".env" in text and "env-github" in text and "view" not in sent
    assert bot.projects_repo.list_for_guild(GUILD) == []


async def test_gitlab_project_next_to_env_gitlab_gets_a_warning(make_bot):  # noqa: F811
    bot = await make_bot(GITLAB_WEBHOOK_SECRET=GITLAB_SECRET, GITLAB_PROJECT_ID="1234")
    sent = await add(bot, url="https://gitlab.com/groupe/projet")
    assert "À vérifier" in embed_text(sent["embed"]) and "view" in sent


async def test_members_are_refused_and_other_servers_too(make_bot):  # noqa: F811
    bot = await make_bot()
    cog = bot.get_cog("ProjectsCog")
    [check] = cog.add.checks
    assert await check(Interaction(bot, FakeGuild(), Member(2))) is False
    other = FakeGuild()
    other.id = OTHER_GUILD
    sent = await add(bot, guild=other, url="https://github.com/equipe/jeu")
    assert "pas encore ouvert" in embed_text(sent["embed"])


async def test_added_project_is_listed_as_pending_and_configurable(make_bot):  # noqa: F811
    bot = await make_bot()
    await confirm(bot, await add(bot, url="https://github.com/equipe/jeu"))
    [record] = bot.projects_repo.list_for_guild(GUILD)
    cog = bot.get_cog("ProjectsCog")
    interaction = Interaction(bot, FakeGuild(), Member())
    await cog.list_projects.callback(cog, interaction)
    assert "pas encore connecté" in embed_text(interaction.response.sent[0]["embed"])
    choices = await cog._registered_autocomplete(Interaction(bot, FakeGuild(), Member(1, admin=True)), "")
    assert [c.value for c in choices] == [str(record.id)]
