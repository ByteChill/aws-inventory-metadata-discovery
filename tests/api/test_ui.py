"""Built-in UI (spec 07 §9). Static routes need no database, so these are unit tests."""

from __future__ import annotations

import re
from typing import Any

from fastapi.testclient import TestClient

from inventory.api.app import UI_DIR, create_app
from inventory.config import ApiSettings
from tests.helpers import assert_matches_fixture, load_fixture

# '/v1/...' string literals and `/v1/...${x}...` templates in app.js
_JS_PATH = re.compile(r"""['`](/v1/[^'`?]*)['`]""")
# every `href: <expr>` in app.js: a literal, or hashFor() which always returns '#/...'
_HREF = re.compile(r"""href:\s*(hashFor\(|['`])""")
_ANY_HREF = re.compile(r"""href:""")


def ui_client(**settings: Any) -> TestClient:
    # no `with`: the lifespan (database, AWS clients) is not needed for static routes
    return TestClient(create_app(ApiSettings(**settings)))


def normalise(path: str) -> str:
    return re.sub(r"\$\{[^}]*\}|\{[^}]*\}", "{}", path)


def app_js() -> str:
    return (UI_DIR / "app.js").read_text(encoding="utf-8")


def test_ui_page_assets_and_security_headers() -> None:
    client = ui_client()
    expected = load_fixture("api/ui.expected.json")

    page = client.get("/ui/")
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    for snippet in expected["page_contains"]:
        assert snippet in page.text
    for name, value in expected["headers"].items():
        assert page.headers[name] == value
    assert page.headers["x-request-id"]

    for asset, content_types in expected["assets"].items():
        response = client.get(f"/ui/{asset}")
        assert response.status_code == 200
        # the JS type depends on the host's mime.types; browsers accept either
        assert response.headers["content-type"].split(";")[0] in content_types
        for name, value in expected["headers"].items():
            assert response.headers[name] == value


def test_redirects_to_ui_are_relative() -> None:
    client = ui_client()
    for path, location in load_fixture("api/ui.expected.json")["redirects"].items():
        response = client.get(path, follow_redirects=False, headers={"Host": "evil.example"})
        assert response.status_code == 307
        assert response.headers["location"] == location


def test_missing_asset_is_a_standard_404() -> None:
    response = ui_client().get(load_fixture("api/ui.expected.json")["missing_asset"])
    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "NOT_FOUND"
    assert error["request_id"] == response.headers["x-request-id"]


def test_ui_can_be_disabled() -> None:
    client = ui_client(ui_enabled=False)
    assert client.get("/ui/").status_code == 404
    assert client.get("/", follow_redirects=False).status_code == 404


def test_ui_is_not_part_of_the_openapi_contract() -> None:
    paths = create_app(ApiSettings()).openapi()["paths"]
    assert not [p for p in paths if p == "/" or p.startswith("/ui")]


def test_ui_only_calls_documented_endpoints() -> None:
    used = sorted({normalise(p) for p in _JS_PATH.findall(app_js())})
    assert_matches_fixture(used, "api/ui.endpoints.expected.json")

    documented = {normalise(p) for p in load_fixture("api/openapi.json")["paths"]}
    assert set(used) <= documented, set(used) - documented


def test_ui_does_not_inject_html() -> None:
    # API data is untrusted (spec 07 §9.2): the page builds DOM nodes, never HTML strings,
    # and every link is built from a fixed '#'/'/' prefix (no javascript: URLs from data)
    source = app_js()
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert sink not in source, sink
    assert len(_HREF.findall(source)) == len(_ANY_HREF.findall(source)) > 0
    assert "return `#/${page}" in source  # hashFor's only return
    for literal in re.findall(r"""href:\s*[`']([^`']*)""", source):
        assert literal.startswith(("#", "/")), literal
