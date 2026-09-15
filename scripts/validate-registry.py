#!/usr/bin/env python3
"""Validate the project registry against schema.json.

Checks every record in ``registry/projects/*.yaml`` plus the reference records in
``registry/examples/``, and enforces the two rules the schema cannot express: the
filename must match ``metadata.name``, and no two records may claim the same name.

Exits non-zero on the first failure so CI fails loudly.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "registry" / "schema.json"
RECORD_DIRS = (
    REPO_ROOT / "registry" / "projects",
    REPO_ROOT / "registry" / "examples",
)


def load_records() -> list[tuple[Path, dict]]:
    records: list[tuple[Path, dict]] = []
    for directory in RECORD_DIRS:
        for path in sorted(directory.glob("*.yaml")):
            with path.open(encoding="utf-8") as handle:
                records.append((path, yaml.safe_load(handle)))
    return records


def validate() -> list[str]:
    validator = Draft202012Validator(
        json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    )
    errors: list[str] = []
    seen: dict[str, Path] = {}

    for path, record in load_records():
        rel = path.relative_to(REPO_ROOT)

        if not isinstance(record, dict):
            errors.append(f"{rel}: not a YAML mapping")
            continue

        for error in sorted(validator.iter_errors(record), key=str):
            location = "/".join(str(part) for part in error.absolute_path)
            errors.append(f"{rel}: {location or '<root>'}: {error.message}")

        name = record.get("metadata", {}).get("name")
        if name is None:
            continue

        # Examples are reference material, not live records: the ApplicationSet and
        # cnp-clean key off the path, so only real records must match their filename
        # and hold a unique name.
        if path.parent.name == "projects":
            if name != path.stem:
                errors.append(
                    f"{rel}: metadata.name is '{name}' but the filename says '{path.stem}'"
                )
            if name in seen:
                errors.append(
                    f"{rel}: project '{name}' is already declared in "
                    f"{seen[name].relative_to(REPO_ROOT)}"
                )
            seen[name] = path

    return errors


def main() -> int:
    errors = validate()
    if errors:
        print(f"✗ {len(errors)} registry problem(s):", file=sys.stderr)
        for error in errors:
            print(f"  {error}", file=sys.stderr)
        return 1

    count = len(load_records())
    print(f"✓ {count} registry record(s) valid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
