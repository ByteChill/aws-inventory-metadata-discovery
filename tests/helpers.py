"""Test helpers. All test data lives under tests/fixtures/."""

from __future__ import annotations

import json
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).parent / "fixtures"
TEXT_SUFFIXES = {".xml", ".html", ".txt", ".pem"}


def fixture_path(rel_path: str) -> Path:
    return FIXTURES / rel_path


def load_fixture(rel_path: str) -> Any:
    path = fixture_path(rel_path)
    if path.suffix == ".json":
        return json.loads(path.read_text(encoding="utf-8"))
    if path.suffix in TEXT_SUFFIXES:
        return path.read_text(encoding="utf-8")
    raise ValueError(f"unsupported fixture type: {rel_path}")


def to_jsonable(value: Any) -> Any:
    """Normalise datetimes/bytes/tuples so values compare equal to parsed JSON."""
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        items = [to_jsonable(v) for v in value]
        return sorted(items, key=json.dumps) if isinstance(value, set | frozenset) else items
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    return value


def assert_matches_fixture(actual: Any, rel_path: str) -> None:
    """Compare against a JSON expectation file. UPDATE_FIXTURES=1 rewrites the file."""
    normalised = to_jsonable(actual)
    path = fixture_path(rel_path)
    if os.environ.get("UPDATE_FIXTURES") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(normalised, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return
    expected = json.loads(path.read_text(encoding="utf-8"))
    assert normalised == expected, f"mismatch with fixture {rel_path}"
