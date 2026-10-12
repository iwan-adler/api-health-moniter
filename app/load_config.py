"""Load endpoints from a JSON file into the database.

Usage:  python -m app.load_config examples/endpoints.json
"""

import json
import os
import sys
from pathlib import Path

from pydantic import ValidationError

from app.db import Database, DuplicateEndpointError
from app.main import DEFAULT_DB_PATH
from app.schemas import EndpointCreate


def load(config_path: Path, db: Database) -> tuple[int, int, list[str]]:
    """Returns (added, skipped_duplicates, errors). Invalid entries are reported, not fatal."""
    entries = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(entries, list):
        raise ValueError("Config file must contain a JSON list of endpoints")

    db.init()
    added, skipped, errors = 0, 0, []
    for index, entry in enumerate(entries):
        try:
            endpoint = EndpointCreate.model_validate(entry)
        except ValidationError as exc:
            errors.append(f"entry {index}: {exc.errors()[0]['msg']}")
            continue
        try:
            db.add_endpoint(
                name=endpoint.name,
                url=str(endpoint.url),
                method=endpoint.method,
                expected_status=endpoint.expected_status,
                timeout_seconds=endpoint.timeout_seconds,
            )
            added += 1
        except DuplicateEndpointError:
            skipped += 1
    return added, skipped, errors


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    db = Database(os.getenv("HEALTH_MONITOR_DB", DEFAULT_DB_PATH))
    added, skipped, errors = load(Path(sys.argv[1]), db)
    print(f"Added {added} endpoint(s), skipped {skipped} duplicate(s) -> {db.path}")
    for error in errors:
        print(f"  invalid {error}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
