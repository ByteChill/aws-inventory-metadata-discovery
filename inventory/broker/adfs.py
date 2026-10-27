"""Standard ADFS forms-based login that yields a base64 SAMLResponse."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin

import httpx
from pydantic import SecretStr

from inventory.broker import BrokerError

log = logging.getLogger(__name__)

USERNAME_FIELD = "UserName"
PASSWORD_FIELD = "Password"


@dataclass
class _Form:
    action: str
    inputs: dict[str, str] = field(default_factory=dict)


class _PageParser(HTMLParser):
    """Collects forms, hidden inputs and the ADFS error text of a page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[_Form] = []
        self.error_text: str | None = None
        self._in_error = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {k.lower(): (v or "") for k, v in attrs}
        if tag == "form":
            self.forms.append(_Form(action=attributes.get("action", "")))
        elif tag == "input" and self.forms and attributes.get("name"):
            unchecked = attributes.get("type", "").lower() in {"checkbox", "radio"} and (
                "checked" not in attributes
            )
            if not unchecked:
                self.forms[-1].inputs[attributes["name"]] = attributes.get("value", "")
        if attributes.get("id") == "errorText":
            self._in_error = True

    def handle_endtag(self, tag: str) -> None:
        self._in_error = False

    def handle_data(self, data: str) -> None:
        if self._in_error and data.strip():
            self.error_text = (self.error_text or "") + data.strip()

    @property
    def saml_response(self) -> str | None:
        for form in self.forms:
            if form.inputs.get("SAMLResponse"):
                return form.inputs["SAMLResponse"]
        return None

    @property
    def login_form(self) -> _Form | None:
        return next(
            (f for f in self.forms if USERNAME_FIELD in f.inputs or PASSWORD_FIELD in f.inputs),
            None,
        )


def _parse(html: str) -> _PageParser:
    parser = _PageParser()
    parser.feed(html)
    parser.close()
    return parser


CredentialLoader = Callable[[], tuple[str, SecretStr]]


class AdfsClient:
    def __init__(
        self,
        idp_url: str,
        credentials: CredentialLoader,
        timeout_seconds: float = 15.0,
        ca_bundle: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """``credentials`` is called on every login so a rotated password is picked up."""
        self._idp_url = idp_url
        self._credentials = credentials
        self._timeout = timeout_seconds
        self._verify: bool | str = ca_bundle or True
        self._transport = transport

    def login(self) -> str:
        """Log in and return the base64 SAMLResponse."""
        try:
            with httpx.Client(
                follow_redirects=True,
                timeout=self._timeout,
                verify=self._verify,
                transport=self._transport,
            ) as client:
                return self._login(client)
        except httpx.HTTPError as exc:
            raise BrokerError(
                "ADFS_UNAVAILABLE", f"ADFS request failed: {type(exc).__name__}"
            ) from exc

    def _login(self, client: httpx.Client) -> str:
        page = client.get(self._idp_url)
        self._check_status(page)
        parsed = _parse(page.text)
        if parsed.saml_response:  # an existing ADFS session cookie can skip the form
            return parsed.saml_response
        form = parsed.login_form
        if form is None:
            raise BrokerError("ADFS_UNAVAILABLE", "ADFS login form not found")

        username, password = self._credentials()
        data = {
            **form.inputs,
            USERNAME_FIELD: username,
            PASSWORD_FIELD: password.get_secret_value(),
            "AuthMethod": form.inputs.get("AuthMethod") or "FormsAuthentication",
        }
        result = client.post(urljoin(str(page.url), form.action or str(page.url)), data=data)
        self._check_status(result)
        parsed = _parse(result.text)
        if parsed.saml_response:
            return parsed.saml_response
        if parsed.login_form is not None and parsed.error_text:
            log.warning("ADFS rejected the credentials", extra={"adfs_error": parsed.error_text})
            raise BrokerError("ADFS_AUTH_FAILED", parsed.error_text)
        raise BrokerError("ADFS_UNAVAILABLE", "ADFS response contained no SAMLResponse")

    @staticmethod
    def _check_status(response: httpx.Response) -> None:
        if response.status_code >= 400:
            raise BrokerError("ADFS_UNAVAILABLE", f"ADFS returned HTTP {response.status_code}")
