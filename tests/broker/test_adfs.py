from __future__ import annotations

from collections.abc import Iterator
from urllib.parse import parse_qs

import httpx
import pytest
import respx
from pydantic import SecretStr

from inventory.broker import BrokerError
from inventory.broker.adfs import AdfsClient
from tests.broker.conftest import saml_b64
from tests.helpers import load_fixture

IDP = (
    "https://adfs.example.invalid/adfs/ls/IdpInitiatedSignOn.aspx?loginToRp=urn:amazon:webservices"
)
POST_URL = "https://adfs.example.invalid/adfs/ls/IdpInitiatedSignOn.aspx"
SECRET = load_fixture("broker/adfs_secret.json")


@pytest.fixture
def router() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as mock:
        yield mock


def client() -> AdfsClient:
    return AdfsClient(
        IDP, lambda: (SECRET["username"], SecretStr(SECRET["password"])), timeout_seconds=2
    )


def saml_page() -> str:
    page: str = load_fixture("broker/adfs/saml_post.html")
    return page.replace("__SAML_RESPONSE__", saml_b64("broker/saml/assertion_single_role.xml"))


def test_login_posts_form_and_returns_saml(router: respx.MockRouter) -> None:
    router.get(IDP).respond(200, html=load_fixture("broker/adfs/login_page.html"))
    post = router.post(url__startswith=POST_URL).respond(200, html=saml_page())

    result = client().login()

    assert result == saml_b64("broker/saml/assertion_single_role.xml")
    form = parse_qs(post.calls.last.request.content.decode())
    assert form["UserName"] == [SECRET["username"]]
    assert form["Password"] == [SECRET["password"]]
    assert form["AuthMethod"] == ["FormsAuthentication"]
    assert "Kmsi" not in form  # unchecked checkbox is not submitted
    assert "client-request-id" in str(post.calls.last.request.url)


def test_existing_session_returns_saml_without_posting(router: respx.MockRouter) -> None:
    router.get(IDP).respond(200, html=saml_page())
    post = router.post(url__startswith=POST_URL)

    assert client().login() == saml_b64("broker/saml/assertion_single_role.xml")
    assert not post.called


def test_wrong_password_is_auth_failure(router: respx.MockRouter) -> None:
    router.get(IDP).respond(200, html=load_fixture("broker/adfs/login_page.html"))
    router.post(url__startswith=POST_URL).respond(
        200, html=load_fixture("broker/adfs/login_failed.html")
    )

    with pytest.raises(BrokerError) as info:
        client().login()

    assert info.value.code == "ADFS_AUTH_FAILED"
    assert not info.value.retryable
    assert "Incorrect user ID or password" in info.value.message


@pytest.mark.parametrize(
    ("status", "page"),
    [(200, "broker/adfs/unexpected.html"), (503, "broker/adfs/unexpected.html")],
)
def test_unexpected_pages_are_unavailable(router: respx.MockRouter, status: int, page: str) -> None:
    router.get(IDP).respond(status, html=load_fixture(page))

    with pytest.raises(BrokerError) as info:
        client().login()

    assert info.value.code == "ADFS_UNAVAILABLE"
    assert info.value.retryable


def test_post_without_saml_or_form_is_unavailable(router: respx.MockRouter) -> None:
    router.get(IDP).respond(200, html=load_fixture("broker/adfs/login_page.html"))
    router.post(url__startswith=POST_URL).respond(
        200, html=load_fixture("broker/adfs/unexpected.html")
    )

    with pytest.raises(BrokerError) as info:
        client().login()
    assert info.value.code == "ADFS_UNAVAILABLE"


def test_network_errors_are_unavailable(router: respx.MockRouter) -> None:
    router.get(IDP).mock(side_effect=httpx.ConnectTimeout("timed out"))

    with pytest.raises(BrokerError) as info:
        client().login()
    assert info.value.code == "ADFS_UNAVAILABLE"


def test_login_form_without_error_text_is_retryable(router: respx.MockRouter) -> None:
    router.get(IDP).respond(200, html=load_fixture("broker/adfs/login_page.html"))
    router.post(url__startswith=POST_URL).respond(
        200, html=load_fixture("broker/adfs/login_page_again.html")
    )

    with pytest.raises(BrokerError) as info:
        client().login()
    assert info.value.code == "ADFS_UNAVAILABLE"
    assert info.value.retryable


def test_credentials_are_loaded_on_every_login(router: respx.MockRouter) -> None:
    router.get(IDP).respond(200, html=load_fixture("broker/adfs/login_page.html"))
    post = router.post(url__startswith=POST_URL).respond(200, html=saml_page())
    passwords = iter(["old-password", "rotated-password"])
    adfs = AdfsClient(IDP, lambda: (SECRET["username"], SecretStr(next(passwords))))

    adfs.login()
    adfs.login()

    sent = [parse_qs(c.request.content.decode())["Password"] for c in post.calls]
    assert sent == [["old-password"], ["rotated-password"]]
