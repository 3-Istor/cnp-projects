#!/usr/bin/env python3
"""Validate the project registry against schema.json.

Checks every record in ``registry/projects/*.yaml`` plus the reference records in
``registry/examples/``, and enforces the two rules the schema cannot express: the
filename must match ``metadata.name``, and no two records may claim the same name.
It also rejects records whose derived Kubernetes and Argo CD names (namespaces,
AppProjects, Applications, hostnames) collide with another source, since a collision
lets one project deploy into another's namespace.

Imported apps (types service and backing) get two more checks: their dependsOn
links stay inside the project and cannot shadow a platform name, and a live
record's imported app has its values file, apps/<project>/<app>/values.yaml,
which is what Argo CD renders it from.

``--self-test`` runs the collision and link checks against in-memory fixtures.

Exits non-zero on the first failure so CI fails loudly.
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "registry" / "schema.json"
VALUES_DIR = REPO_ROOT / "apps"
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
IMPORTED_TYPES = ("service", "backing")
# An alias is a Service name in the caller's namespace: these would shadow the
# names an app uses to reach the platform (D18). Dots never pass the schema, so
# no alias can spell a FQDN such as vault.vault.svc.cluster.local.
RESERVED_ALIASES = frozenset({"kubernetes", "default", "vault", "keycloak", "argocd"})
# Imported app names and aliases become Service names: DNS-1035, so a letter first.
DNS_1035_LABEL = re.compile(r"^[a-z]([-a-z0-9]*[a-z0-9])?$")
# The chart names the canary Services <app>-stable and <app>-canary.
OWN_SERVICE_SUFFIXES = ("", "-stable", "-canary")


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
        # Every environment, staging or not: the AppProjects always permit both
        # namespaces (so turning staging off can delete what it created), so
        # both names belong to this app.
        for environment in ENVIRONMENT_SUFFIX:
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
        namespace = f"{project}-{app_name}"
        staging = f"{namespace}{ENVIRONMENT_SUFFIX['staging']}"
        if len(staging) > MAX_LABEL_LENGTH:
            errors.append(
                f"{label}: app '{app_name}' gives staging namespace '{staging}' of "
                f"{len(staging)} characters, the limit is {MAX_LABEL_LENGTH}"
            )
        elif not DNS_LABEL.match(namespace):
            errors.append(
                f"{label}: app '{app_name}' gives namespace '{namespace}' which is "
                f"not a valid DNS-1123 label (lowercase alphanumerics and '-' only)"
            )
    return errors


def check_imported_apps(label: str, apps: list[dict]) -> list[str]:
    """Links target an imported app of the same record; routes need public."""
    errors: list[str] = []
    types = {app["name"]: app.get("type") for app in apps}
    for app in apps:
        if app.get("type") not in IMPORTED_TYPES:
            continue
        app_name = app["name"]
        if not DNS_1035_LABEL.match(app_name):
            errors.append(
                f"{label}: app '{app_name}' is not a DNS-1035 label: it must start with a letter"
            )
        if app.get("public") and not (app.get("hostnames") or {}).get("prod"):
            errors.append(f"{label}: app '{app_name}' is public but has no hostnames.prod")
        if not app.get("public") and app.get("hostnames"):
            errors.append(
                f"{label}: app '{app_name}' has hostnames but is not public: "
                f"only a public imported app gets a route"
            )
        aliases: set[str] = set()
        for link in app.get("dependsOn") or []:
            if not isinstance(link, dict) or not isinstance(link.get("app"), str):
                continue
            target = link["app"]
            alias = link.get("alias") or target
            if not DNS_1035_LABEL.match(alias):
                errors.append(
                    f"{label}: app '{app_name}' uses alias '{alias}', which is not a "
                    f"DNS-1035 label: it must start with a letter"
                )
            if target == app_name:
                errors.append(f"{label}: app '{app_name}' depends on itself")
            elif target not in types:
                errors.append(
                    f"{label}: app '{app_name}' depends on '{target}', which is not an "
                    f"app of this project"
                )
            elif types[target] not in IMPORTED_TYPES:
                errors.append(
                    f"{label}: app '{app_name}' depends on '{target}' of type "
                    f"{types[target]}: links target service or backing apps only"
                )
            if alias in RESERVED_ALIASES:
                errors.append(f"{label}: app '{app_name}' uses reserved alias '{alias}'")
            if alias in {app_name + suffix for suffix in OWN_SERVICE_SUFFIXES}:
                errors.append(
                    f"{label}: app '{app_name}' uses alias '{alias}', which is its own Service"
                )
            if alias in aliases:
                errors.append(f"{label}: app '{app_name}' declares alias '{alias}' twice")
            aliases.add(alias)
    return errors


def service_ports(values_file: Path) -> set[int] | None:
    """The target's service.ports[].port, or None when its values do not say."""
    if not values_file.is_file():
        return None
    values = yaml.safe_load(values_file.read_text(encoding="utf-8")) or {}
    ports = (values.get("service") or {}).get("ports") if isinstance(values, dict) else None
    if not isinstance(ports, list):
        return None
    return {port["port"] for port in ports if isinstance(port, dict) and "port" in port}


def check_link_ports(label: str, project: str, apps: list[dict], values_dir: Path = VALUES_DIR) -> list[str]:
    """A link's port must be one the target's Service exposes, when its values are here."""
    errors: list[str] = []
    for app in apps:
        if app.get("type") not in IMPORTED_TYPES:
            continue
        for link in app.get("dependsOn") or []:
            if not isinstance(link, dict) or not isinstance(link.get("app"), str):
                continue
            ports = service_ports(values_dir / project / link["app"] / "values.yaml")
            if ports is not None and link.get("port") not in ports:
                errors.append(
                    f"{label}: app '{app['name']}' links to '{link['app']}' on port "
                    f"{link.get('port')}, which its service.ports do not expose "
                    f"({', '.join(str(p) for p in sorted(ports)) or 'none'})"
                )
    return errors


def check_values_files(label: str, project: str, apps: list[dict]) -> list[str]:
    """Argo CD renders an imported app from its values file: it must exist (D2)."""
    errors: list[str] = []
    for app in apps:
        if app.get("type") not in IMPORTED_TYPES:
            continue
        values = VALUES_DIR / project / app["name"] / "values.yaml"
        if not values.is_file():
            errors.append(
                f"{label}: app '{app['name']}' has no {values.relative_to(REPO_ROOT)}"
            )
    return errors


def check_environments(label: str, record: dict) -> list[str]:
    """Staging stays under the project's app cap. Must match the CMP."""
    staging = next(
        (
            env
            for env in record.get("spec", {}).get("environments") or []
            if isinstance(env, dict) and env.get("name") == "staging"
        ),
        {},
    )
    # A project without a staging entry gets one app; maxApps 0 forbids it.
    max_apps = staging.get("maxApps", 1)
    staging_apps = [
        app["name"] for app in usable_apps(record) if "staging" in app_environments(app)
    ]
    if len(staging_apps) > max_apps:
        return [
            f"{label}: {len(staging_apps)} apps use staging, the project "
            f"allows {max_apps} (spec.environments[staging].maxApps)"
        ]
    return []


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
        errors.extend(check_imported_apps(str(rel), usable_apps(record)))
        errors.extend(check_link_ports(str(rel), name, usable_apps(record)))

        if path.parent.name == "projects":
            live.append((str(rel), record))
            errors.extend(check_values_files(str(rel), name, usable_apps(record)))
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
        "68 characters",
    )
    expect(
        "namespace at 63 characters, staging suffix included",
        check_app_names("a.yaml", "p" * 40, [app("a" * 18)]),
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
        "the staging name is reserved even without staging",
        collisions(
            ("a.yaml", record("foo", app("web"))),
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
        "one staging app without a project cap",
        check_environments("a.yaml", staged([{"name": "prod"}], app("web", environments=["prod", "staging"]))),
    )
    expect(
        "two staging apps without a project cap",
        check_environments(
            "a.yaml",
            staged(
                [{"name": "prod"}],
                app("web", environments=["prod", "staging"]),
                app("api", environments=["prod", "staging"]),
            ),
        ),
        "2 apps use staging",
    )
    expect(
        "staging forbidden by a zero cap",
        check_environments(
            "a.yaml",
            staged(
                [{"name": "prod"}, {"name": "staging", "maxApps": 0}],
                app("web", environments=["prod", "staging"]),
            ),
        ),
        "allows 0",
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

    def imported(name: str, type_: str = "service", **fields) -> dict:
        return {"name": name, "type": type_, **fields}

    def link(target: str, port: int = 8080, alias: str | None = None) -> dict:
        result = {"app": target, "port": port}
        if alias:
            result["alias"] = alias
        return result

    expect(
        "service linked to a backing app of the same project",
        check_imported_apps(
            "a.yaml",
            [
                imported("api", dependsOn=[link("mongodb", 27017), link("cache", 6379, "redis")]),
                imported("mongodb", "backing"),
                imported("cache", "backing"),
            ],
        ),
    )
    expect(
        "link to an app outside the project",
        check_imported_apps("a.yaml", [imported("api", dependsOn=[link("vault", 8200, "secrets")])]),
        "'vault', which is not an app of this project",
    )
    expect(
        "link to a template app",
        check_imported_apps(
            "a.yaml", [imported("api", dependsOn=[link("web")]), app("web", "fullstack")]
        ),
        "of type fullstack",
    )
    expect(
        "link to itself",
        check_imported_apps("a.yaml", [imported("api", dependsOn=[link("api")])]),
        "depends on itself",
    )
    for reserved in sorted(RESERVED_ALIASES):
        expect(
            f"reserved alias {reserved}",
            check_imported_apps(
                "a.yaml",
                [imported("api", dependsOn=[link("db", 5432, reserved)]), imported("db", "backing")],
            ),
            f"reserved alias '{reserved}'",
        )
    expect(
        "alias equal to the app's own name",
        check_imported_apps(
            "a.yaml",
            [imported("api", dependsOn=[link("db", 5432, "api")]), imported("db", "backing")],
        ),
        "its own Service",
    )
    expect(
        "alias declared twice",
        check_imported_apps(
            "a.yaml",
            [
                imported("api", dependsOn=[link("db", 5432, "store"), link("cache", 6379, "store")]),
                imported("db", "backing"),
                imported("cache", "backing"),
            ],
        ),
        "alias 'store' twice",
    )
    expect(
        "public service without a hostname",
        check_imported_apps("a.yaml", [imported("api", public=True)]),
        "no hostnames.prod",
    )
    expect(
        "private service with a hostname",
        check_imported_apps("a.yaml", [imported("api", hostnames={"prod": "api-foo.3istor.com"})]),
        "not public",
    )
    expect(
        "public service with its hostname",
        check_imported_apps(
            "a.yaml", [imported("api", public=True, hostnames={"prod": "api-foo.3istor.com"})]
        ),
    )
    expect(
        "imported app name starting with a digit",
        check_imported_apps("a.yaml", [imported("9api")]),
        "'9api' is not a DNS-1035 label",
    )
    expect(
        "alias starting with a digit",
        check_imported_apps(
            "a.yaml",
            [imported("api", dependsOn=[link("db", 5432, "1db")]), imported("db", "backing")],
        ),
        "alias '1db', which is not a DNS-1035 label",
    )
    for suffix in ("-canary", "-stable"):
        expect(
            f"alias equal to the app's own {suffix} Service",
            check_imported_apps(
                "a.yaml",
                [imported("api", dependsOn=[link("db", 5432, "api" + suffix)]), imported("db", "backing")],
            ),
            "its own Service",
        )

    with tempfile.TemporaryDirectory() as tmp:
        values_dir = Path(tmp)
        (values_dir / "foo" / "db").mkdir(parents=True)
        (values_dir / "foo" / "db" / "values.yaml").write_text(
            "service:\n  ports:\n    - {name: tcp, port: 5432, targetPort: 5432}\n", encoding="utf-8"
        )
        linked = [imported("api", dependsOn=[link("db", 5432)]), imported("db", "backing")]
        expect("link on a port the target exposes", check_link_ports("a.yaml", "foo", linked, values_dir))
        expect(
            "link on a port the target does not expose",
            check_link_ports(
                "a.yaml", "foo", [imported("api", dependsOn=[link("db", 27017)]), imported("db", "backing")], values_dir
            ),
            "port 27017, which its service.ports do not expose (5432)",
        )
        expect(
            "link to a target without values here",
            check_link_ports(
                "a.yaml", "foo", [imported("api", dependsOn=[link("cache", 1)]), imported("cache", "backing")], values_dir
            ),
        )

    expect(
        "imported app without its values file",
        check_values_files("a.yaml", "no-such-project", [imported("api")]),
        "apps/no-such-project/api/values.yaml",
    )
    expect(
        "template apps need no values file here",
        check_values_files("a.yaml", "no-such-project", [app("web")]),
    )

    validator = Draft202012Validator(json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))
    sha = "0123456789abcdef0123456789abcdef01234567"
    source = {"repo": "https://github.com/acme/shop", "path": "services/api", "sha": sha}

    def schema_errors(*apps: dict) -> list[str]:
        record = {
            "apiVersion": "cnp.3istor.com/v1alpha1",
            "kind": "ProjectRecord",
            "metadata": {"name": "foo"},
            "spec": {"targetCloud": "onprem", "apps": list(apps)},
        }
        return [error.message for error in validator.iter_errors(record)]

    def refused(description: str, errors: list[str]) -> None:
        if not errors:
            failures.append(f"{description}: expected a schema error, got none")

    expect(
        "schema accepts a service and a backing app",
        schema_errors(
            imported("api", source=source, dependsOn=[link("mongodb", 27017)]),
            imported("mongodb", "backing"),
            imported(
                "web",
                source=source,
                public=True,
                hostnames={"prod": "web-foo.3istor.com"},
            ),
        ),
    )
    expect(
        "schema keeps template apps valid",
        schema_errors(app("web", "fullstack"), app("docs")),
    )
    refused("service with a repoURL", schema_errors(imported("api", source=source, repoURL="https://github.com/acme/shop")))
    refused("service without a source", schema_errors(imported("api")))
    refused("source sha that is not a full commit", schema_errors(imported("api", source={**source, "sha": "abc123"})))
    refused("backing app with a source", schema_errors(imported("db", "backing", source=source)))
    refused("public backing app", schema_errors(imported("db", "backing", public=True)))
    refused("imported app on staging", schema_errors(imported("db", "backing", environments=["prod", "staging"])))
    refused("imported app name over 40 characters", schema_errors(imported("d" * 41, "backing")))
    refused("dependsOn on a template app", schema_errors({**app("web"), "dependsOn": [link("db")]}))
    refused("public on a template app", schema_errors({**app("web"), "public": True}))
    refused("alias with dots", schema_errors(imported("api", source=source, dependsOn=[link("db", 5432, "vault.vault.svc.cluster.local")])))
    refused("link without a port", schema_errors(imported("api", source=source, dependsOn=[{"app": "db"}])))
    refused("imported app name starting with a digit", schema_errors(imported("9db", "backing")))
    refused("dependsOn app starting with a digit", schema_errors(imported("api", source=source, dependsOn=[link("9db")])))
    refused("alias starting with a digit", schema_errors(imported("api", source=source, dependsOn=[link("db", 5432, "1db")])))
    refused("port out of range", schema_errors(imported("api", source=source, dependsOn=[link("db", 70000)])))

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
