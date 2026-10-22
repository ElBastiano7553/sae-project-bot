"""Client API GitLab : contre un faux serveur local, sans réseau ni vrai token."""

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from src.gitlab.api import GitLabAPI, GitLabAPIError

TOKEN = "jeton-factice-de-test-0000"


@pytest.fixture
async def server():
    seen = []

    async def mr(request):
        seen.append(request.headers.get("PRIVATE-TOKEN"))
        if request.match_info["iid"] == "404":
            return web.json_response({"message": "404 Not found"}, status=404)
        if request.match_info["iid"] == "401":
            return web.json_response({"message": "401 Unauthorized"}, status=401)
        return web.json_response({
            "iid": 12, "title": "Ajout du score", "description": "Ferme #3", "state": "opened",
            "author": {"name": "Bob Durand"}, "source_branch": "feature/score", "target_branch": "main",
            "web_url": "https://gitlab.com/epita/sae-game/-/merge_requests/12",
        })

    async def diffs(request):
        assert request.query["per_page"] == "100"
        return web.json_response([
            {"old_path": "src/score.py", "new_path": "src/score.py", "diff": "@@ -1 +1 @@\n-a\n+b\n"},
            {"old_path": "old.py", "new_path": "new.py", "renamed_file": True, "diff": ""},
            "inattendu",
        ])

    app = web.Application()
    app.router.add_get("/api/v4/projects/42/merge_requests/{iid}", mr)
    app.router.add_get("/api/v4/projects/42/merge_requests/{iid}/diffs", diffs)
    test_server = TestServer(app)
    await test_server.start_server()
    test_server.seen = seen
    yield test_server
    await test_server.close()


@pytest.fixture
async def api(server):
    client = GitLabAPI(str(server.make_url("")), TOKEN, 42)
    yield client
    await client.close()


async def test_merge_request_and_diffs(api, server):
    mr = await api.merge_request(12)
    assert (mr.iid, mr.title, mr.author, mr.target_branch) == (12, "Ajout du score", "Bob Durand", "main")
    assert server.seen == [TOKEN]  # le token n'est envoyé que dans l'en-tête prévu
    diffs = await api.merge_request_diffs(12)
    assert [d.path for d in diffs] == ["src/score.py", "new.py"]
    assert diffs[1].renamed_from == "old.py"


@pytest.mark.parametrize("iid, message", [(404, "ne trouve pas"), (401, "refuse le token")])
async def test_errors_are_readable_and_never_contain_the_token(api, iid, message):
    with pytest.raises(GitLabAPIError, match=message) as exc:
        await api.merge_request(iid)
    assert TOKEN not in str(exc.value)


async def test_unreachable_gitlab():
    client = GitLabAPI("http://127.0.0.1:9", TOKEN, 42)
    try:
        with pytest.raises(GitLabAPIError, match="injoignable"):
            await client.merge_request(1)
    finally:
        await client.close()
