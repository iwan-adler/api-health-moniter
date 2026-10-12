import json
from pathlib import Path

import pytest

from app.db import Database
from app.load_config import load


def write(tmp_path, data) -> Path:
    path = tmp_path / "endpoints.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_loads_valid_entries_and_reports_invalid(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    config = write(
        tmp_path,
        [
            {"name": "ok", "url": "https://up.test/"},
            {"name": "bad", "url": "not-a-url"},
            {"name": "ok2", "url": "https://up.test/2", "method": "HEAD"},
        ],
    )

    added, skipped, errors = load(config, db)

    assert (added, skipped) == (2, 0)
    assert len(errors) == 1 and errors[0].startswith("entry 1")
    assert [e["name"] for e in db.list_endpoints()] == ["ok", "ok2"]


def test_loading_twice_skips_duplicates(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    config = write(tmp_path, [{"name": "ok", "url": "https://up.test/"}])

    load(config, db)
    assert load(config, db) == (0, 1, [])
    assert len(db.list_endpoints()) == 1


def test_rejects_non_list(tmp_path):
    with pytest.raises(ValueError):
        load(write(tmp_path, {"name": "x"}), Database(str(tmp_path / "t.db")))


def test_example_config_is_valid(tmp_path):
    added, skipped, errors = load(
        Path("examples/endpoints.json"), Database(str(tmp_path / "t.db"))
    )
    assert errors == [] and added == 6
