#!/usr/bin/env python3
"""Validate the project registry against schema.json.

Checks every record in ``registry/projects/*.yaml`` plus the reference records in
``registry/examples/``, and enforces the two rules the schema cannot express: the
filename must match ``metadata.name``, and no two records may claim the same name.
It also rejects records whose derived Kubernetes and Argo CD names (namespaces,
AppProjects, Applications, hostnames) collide with another source, since a collision
lets one project deploy into another's namespace.

``--self-test`` runs the collision checks against in-memory fixtures.

Exits non-zero on the first failure so CI fails loudly.
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "registry" / "schema.json"
RECORD_DIRS = (
    REPO_ROOT / "registry" / "projects",
    REPO_ROOT / "registry" / "examples",
)
DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
MAX_LABEL_LENGTH = 63
FULLSTACK_COMPONENTS = ("frontend", "backend")
# Prod keeps the names every existing app already has; staging objects carry a
# suffix. Must match the ApplicationSets and cnp-project-base.
ENVIRONMENT_SUFFIX = {"prod": "", "staging": "-stg"}


def app_environments(app: dict) -> list[str]:
    environments = app.get("environments") or ["prod"]
    return [env for env in environments if env in ENVIRONMENT_SUFFIX]


def derive_names(project: str, apps: list[dict]) -> list[tuple[str, str, str]]:
    """Return (kind, name, source) for every name the ApplicationSets derive."""
    names = [
        ("namespace", f"{project}-system", "the project system namespace"),
        ("appproject", project, "the project AppProject"),
        ("appproject", f"{project}-platform", "the platform AppProject"),
        ("application", f"{project}-appproject", "the appproject Application"),
        ("application", f"{project}-bootstrap", "the bootstrap Application"),
        ("hostname", f"status-{project}.3istor.com", "the status hostname"),
        ("hostname", f"offhours-{project}.3istor.com", "the offhours hostname"),
        ("hostname", f"auth-{project}.3istor.com", "the auth hostname"),
    ]
    for app in apps:
        app_name = app["name"]
        components = (
            FULLSTACK_COMPONENTS if app.get("type") == "fullstack" else ("app",)
        )
        for environment in app_environments(app):
            suffix = ENVIRONMENT_SUFFIX[environment]
            names.append(
                (
                    "namespace",
                    f"{project}-{app_name}{suffix}",
                    f"the {environment} namespace of app '{app_name}'",
                )
            )
            for component in components:
                names.append(
                    (
                        "application",
                        f"{project}-{app_name}-{component}{suffix}",
                        f"the {environment} {component} Application of app '{app_name}'",
                    )
                )
        for environment, hostname in (app.get("hostnames") or {}).items():
            names.append(
                (
                    "hostname",
                    str(hostname).lower(),
                    f"the {environment} hostname of app '{app_name}'",
                )
            )
    return names


def usable_apps(record: dict) -> list[dict]:
    apps = record.get("spec", {}).get("apps", [])
    if not isinstance(apps, list):
        return []
    return [
        app
        for app in apps
        if isinstance(app, dict) and isinstance(app.get("name"), str)
    ]


def check_app_names(label: str, project: str, apps: list[dict]) -> list[str]:
    errors: list[str] = []
    for app in apps:
        app_name = app["name"]
        if app_name == "system":
            errors.append(
                f"{label}: app 'system' is reserved: namespace '{project}-system' "
                f"belongs to the project itself"
            )
        namespace = max(
            (f"{project}-{app_name}{ENVIRONMENT_SUFFIX[env]}" for env in app_environments(app)),
            key=len,
            default=f"{project}-{app_name}",
        )
        if len(namespace) > MAX_LABEL_LENGTH:
            errors.append(
                f"{label}: app '{app_name}' gives namespace '{namespace}' of "
                f"{len(namespace)} characters, the limit is {MAX_LABEL_LENGTH}"
            )
        elif not DNS_LABEL.match(namespace):
            errors.append(
                f"{label}: app '{app_name}' gives namespace '{namespace}' which is "
                f"not a valid DNS-1123 label (lowercase alphanumerics and '-' only)"
            )
    return errors


def check_environments(label: str, record: dict) -> list[str]:
    """Staging must be allowed by the project and stay under its app cap."""
    spec = record.get("spec", {})
    allowed = {
        env.get("name"): env
        for env in spec.get("environments") or [{"name": "prod"}]
        if isinstance(env, dict)
    }
    errors: list[str] = []
    staging_apps = [
        app["name"] for app in usable_apps(record) if "staging" in app_environments(app)
    ]
    if staging_apps and "staging" not in allowed:
        errors.append(
            f"{label}: apps {staging_apps} use staging but the project's "
            f"spec.environments does not list it"
        )
    elif staging_apps:
        max_apps = allowed["staging"].get("maxApps", 1)
        if len(staging_apps) > max_apps:
            errors.append(
                f"{label}: {len(staging_apps)} apps use staging, the project "
                f"allows {max_apps} (spec.environments[staging].maxApps)"
            )
    return errors


def check_collisions(records: list[tuple[str, dict]]) -> list[str]:
    owners: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    for label, record in records:
        project = record.get("metadata", {}).get("name")
        if not isinstance(project, str):
            continue
        for kind, name, source in derive_names(project, usable_apps(record)):
            owners[(kind, name)].append((label, source))

    errors: list[str] = []
    for (kind, name), claims in sorted(owners.items()):
        for label, source in claims[1:]:
            first_label, first_source = claims[0]
            errors.append(
                f"{label}: {kind} '{name}' from {source} collides with "
                f"{first_source} in {first_label}"
            )
    return errors


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
    live: list[tuple[str, dict]] = []

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
        errors.extend(check_app_names(str(rel), name, usable_apps(record)))
        errors.extend(check_environments(str(rel), record))

        if path.parent.name == "projects":
            live.append((str(rel), record))
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

    errors.extend(check_collisions(live))
    return errors


def self_test() -> int:
    def record(name: str, *apps: dict) -> dict:
        return {"metadata": {"name": name}, "spec": {"apps": list(apps)}}

    def app(
        name: str,
        type_: str = "static",
        environments: list[str] | None = None,
        **hostnames: str,
    ) -> dict:
        result = {"name": name, "type": type_, "hostnames": hostnames}
        if environments:
            result["environments"] = environments
        return result

    def staged(project_envs: list[dict], *apps: dict) -> dict:
        return {
            "metadata": {"name": "foo"},
            "spec": {"environments": project_envs, "apps": list(apps)},
        }

    def collisions(*pairs: tuple[str, dict]) -> list[str]:
        return check_collisions(list(pairs))

    failures: list[str] = []

    def expect(description: str, errors: list[str], *fragments: str) -> None:
        text = "\n".join(errors)
        if fragments and not all(fragment in text for fragment in fragments):
            failures.append(f"{description}: expected {fragments}, got {errors}")
        if not fragments and errors:
            failures.append(f"{description}: expected no error, got {errors}")

    expect(
        "app namespace equals another project's system namespace",
        collisions(
            ("a.yaml", record("foo", app("bar-system"))),
            ("foo-bar.yaml", record("foo-bar")),
        ),
        "namespace 'foo-bar-system'",
        "a.yaml",
        "foo-bar.yaml",
    )
    expect(
        "two app namespaces coincide",
        collisions(
            ("a.yaml", record("foo", app("bar-baz"))),
            ("b.yaml", record("foo-bar", app("baz"))),
        ),
        "namespace 'foo-bar-baz'",
    )
    expect(
        "project named like another's platform AppProject",
        collisions(
            ("foo.yaml", record("foo")),
            ("foo-platform.yaml", record("foo-platform")),
        ),
        "appproject 'foo-platform'",
        "foo.yaml",
        "foo-platform.yaml",
    )
    expect(
        "application names coincide",
        collisions(
            ("a.yaml", record("foo", app("bar-app", "fullstack"))),
            ("b.yaml", record("foo-bar", app("app", "fullstack"))),
        ),
        "application 'foo-bar-app-frontend'",
    )
    expect(
        "app hostname equals a derived project hostname",
        collisions(
            ("a.yaml", record("foo", app("web", prod="status-bar.3istor.com"))),
            ("bar.yaml", record("bar")),
        ),
        "hostname 'status-bar.3istor.com'",
    )
    expect(
        "duplicate app names inside one record",
        collisions(("a.yaml", record("foo", app("web"), app("web")))),
        "namespace 'foo-web'",
        "a.yaml",
    )
    expect(
        "unrelated records",
        collisions(
            ("a.yaml", record("foo", app("web", prod="web-foo.3istor.com"))),
            ("b.yaml", record("bar", app("web", prod="web-bar.3istor.com"))),
        ),
    )
    expect(
        "app named system",
        check_app_names("a.yaml", "foo", [app("system")]),
        "app 'system' is reserved",
    )
    expect(
        "namespace over 63 characters",
        check_app_names("a.yaml", "p" * 40, [app("a" * 23)]),
        "64 characters",
    )
    expect(
        "namespace at 63 characters",
        check_app_names("a.yaml", "p" * 40, [app("a" * 22)]),
    )
    expect(
        "uppercase app name",
        check_app_names("a.yaml", "foo", [app("Web")]),
        "not a valid DNS-1123 label",
    )
    expect(
        "dotted app name",
        check_app_names("a.yaml", "foo", [app("web.v2")]),
        "not a valid DNS-1123 label",
    )
    expect(
        "trailing hyphen app name",
        check_app_names("a.yaml", "foo", [app("web-")]),
        "not a valid DNS-1123 label",
    )

    expect(
        "staging namespace collides with another app's prod namespace",
        collisions(
            ("a.yaml", record("foo", app("web", environments=["prod", "staging"]))),
            ("b.yaml", record("foo-web", app("stg"))),
        ),
        "namespace 'foo-web-stg'",
    )
    expect(
        "staging Application names stay distinct from prod",
        collisions(
            ("a.yaml", record("foo", app("web", "fullstack", ["prod", "staging"]))),
        ),
    )
    expect(
        "staging namespace over 63 characters",
        check_app_names("a.yaml", "p" * 40, [app("a" * 20, environments=["prod", "staging"])]),
        "65 characters",
    )
    expect(
        "staging not allowed by the project",
        check_environments("a.yaml", staged([{"name": "prod"}], app("web", environments=["prod", "staging"]))),
        "does not list it",
    )
    expect(
        "staging over the project cap",
        check_environments(
            "a.yaml",
            staged(
                [{"name": "prod"}, {"name": "staging", "maxApps": 1}],
                app("web", environments=["prod", "staging"]),
                app("api", environments=["prod", "staging"]),
            ),
        ),
        "2 apps use staging",
    )
    expect(
        "staging within the project cap",
        check_environments(
            "a.yaml",
            staged(
                [{"name": "prod"}, {"name": "staging"}],
                app("web", environments=["prod", "staging"]),
                app("api"),
            ),
        ),
    )

    if failures:
        for failure in failures:
            print(f"  FAIL {failure}", file=sys.stderr)
        return 1
    print("✓ self-test passed")
    return 0


def main() -> int:
    if sys.argv[1:] == ["--self-test"]:
        return self_test()

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
