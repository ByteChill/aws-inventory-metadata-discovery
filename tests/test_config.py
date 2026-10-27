from __future__ import annotations

import pytest
from pydantic import ValidationError

from inventory.config import CommonSettings, clear_settings_cache, get_common_settings
from inventory.errors import NotImplementedFeatureError
from tests.helpers import load_fixture


def test_parses_cells_from_json_env(settings_env: dict[str, str]) -> None:
    settings = get_common_settings()

    assert settings.supported_regions == ["ap-east-1", "eu-west-1", "eu-west-2", "us-east-1"]
    assert settings.cell("eu-west-1").endpoints["sqs"].startswith("https://vpce-0a1-sqs")
    # 'global' resolves to the global home region's cell
    assert settings.cell("global").region == "us-east-1"


def test_missing_cell_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in load_fixture("foundation/settings.missing_cell.json").items():
        monkeypatch.setenv(key, value)

    with pytest.raises(ValidationError, match="supported regions without a cell"):
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


def test_unknown_cell_raises(settings_env: dict[str, str]) -> None:
    with pytest.raises(KeyError):
        get_common_settings().cell("sa-east-1")
