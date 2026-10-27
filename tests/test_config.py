from __future__ import annotations

import pytest
from pydantic import ValidationError

from inventory.config import CommonSettings, clear_settings_cache, get_common_settings
from inventory.errors import NotImplementedFeatureError
from tests.helpers import load_fixture


def test_parses_regions_and_endpoints_from_env(settings_env: dict[str, str]) -> None:
    settings = get_common_settings()

    assert settings.primary_region == "ap-east-1"
    assert settings.scan_regions == ["ap-east-1", "eu-west-1", "eu-west-2", "us-east-1"]
    assert settings.endpoints["eu-west-1"]["sqs"].startswith("https://vpce-0a1-sqs")
    assert settings.broker_function_name == "inventory-local-adfs-broker"
    assert settings.queue_name("ondemand") == "inventory-local-ondemand"


def test_scan_regions_must_include_us_east_1(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in load_fixture("foundation/settings.no_us_east_1.json").items():
        monkeypatch.setenv(key, value)

    with pytest.raises(ValidationError, match="must include us-east-1"):
        CommonSettings()


@pytest.mark.parametrize(
    ("env", "value"),
    [("INVENTORY_TRANSPORT", "lambda"), ("INVENTORY_HISTORY_SINK", "outbox")],
)
def test_reserved_features_fail_fast(
    settings_env: dict[str, str], monkeypatch: pytest.MonkeyPatch, env: str, value: str
) -> None:
    monkeypatch.setenv(env, value)
    clear_settings_cache()

    with pytest.raises(NotImplementedFeatureError):
        get_common_settings()
