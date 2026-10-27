"""Serving the built web UI (spec 10 §6) and checks on its source. Static routes need no
database, so these are unit tests; a tiny fixture build stands in for web/dist."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from inventory.api.app import create_app
from inventory.config import ApiSettings
from tests.helpers import assert_matches_fixture, fixture_path, load_fixture

WEB_SRC = Path(__file__).parents[2] / "web" / "src"
FIXTURE_UI = str(fixture_path("api/ui"))
# '/v1/...' string literals and `/v1/...${x}...` templates in the API client
_JS_PATH = re.compile(r"""['`](/v1/[^'`?]*)['`]""")


def ui_client(**settings: Any) -> TestClient:
    # no `with`: the lifespan (database, AWS clients) is not needed for static routes
    return TestClient(create_app(ApiSettings(**({"ui_dir": FIXTURE_UI} | settings))))


def normalise(path: str) -> str:
    return re.sub(r"\$\{[^}]*\}|\{[^}]*\}", "{}", path)


def web_sources() -> dict[str, str]:
    return {
        str(p.relative_to(WEB_SRC)): p.read_text(encoding="utf-8") for p in WEB_SRC.rglob("*.ts*")
    }


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


def test_unbuilt_ui_is_not_mounted(tmp_path: Path) -> None:
    client = ui_client(ui_dir=str(tmp_path))
    assert client.get("/ui/").status_code == 404
    assert client.get("/healthz").status_code == 200


def test_ui_is_not_part_of_the_openapi_contract() -> None:
    paths = create_app(ApiSettings(ui_dir=FIXTURE_UI)).openapi()["paths"]
    assert not [p for p in paths if p == "/" or p.startswith("/ui")]


def test_ui_only_calls_documented_endpoints() -> None:
    used = sorted({normalise(p) for src in web_sources().values() for p in _JS_PATH.findall(src)})
    assert_matches_fixture(used, "api/ui.endpoints.expected.json")

    documented = {normalise(p) for p in load_fixture("api/openapi.json")["paths"]}
    assert set(used) <= documented, set(used) - documented


def test_ui_does_not_inject_html() -> None:
    # API data is untrusted (spec 10 §5): React renders it as text, never as HTML
    for name, source in web_sources().items():
        for sink in (
            "dangerouslySetInnerHTML",
            "innerHTML",
            "outerHTML",
            "document.write",
            "eval(",
        ):
            assert sink not in source, (name, sink)
