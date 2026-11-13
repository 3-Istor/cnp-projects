# Project registry

One file per project, in `registry/projects/<name>.yaml`. This is **the source of truth
for where a project runs** — decision D-01 of the multicloud chantier.

Git holds the authority, not the CMP database. The database carries a mirror of
`targetCloud` so the portal can render a project list without reading Git on every
request; if the two ever disagree, the file wins. The commit history is the point: it
gives review, blame and rollback on infrastructure placement for free.

## Who reads this

| Consumer | What it uses |
| --- | --- |
| Argo CD `ApplicationSet` | `spec.targetCloud` becomes `destination.name`; `spec.apps` generates the Applications |
| `cnp-project-base` | `spec.features` decides which per-project add-ons render |
| blackbox probe target list | `spec.apps[].hostnames` |
| `cnp-clean` | the inventory of what belongs to a project, and which cloud to look on |
| CMP portal | mirrored into the `projects` table for listing |

## Who writes this

The CMP API, on project creation, via the GitHub App. Hand edits are legitimate for
operational changes (suspending a project, flipping a feature flag) — that is the whole
reason the registry is a Git file and not a database row.

## What this directory is *not*

`registry/projects/` holds **CNP records**, not Kubernetes manifests. The sibling
`projects/` directory at the repo root is a different thing: it is synced directly as
manifests by the `cnp-projects-root` Argo CD Application. Do not put records there —
Argo CD would try to apply them as resources and fail.

That split is deliberate and temporary. Once the `ApplicationSet` (WS-3) generates every
project Application from this registry, the hand-written manifests in `projects/` are
migrated across and that directory goes away.

## Example

```yaml
apiVersion: cnp.3istor.com/v1alpha1
kind: ProjectRecord
metadata:
  name: sandbox
  owner: bperret
  createdAt: "2026-09-15T10:00:00Z"
spec:
  targetCloud: onprem
  status: active
  environments:
    - name: prod
      dbInstances: 1
  apps:
    - name: demo-app
      type: fullstack
      repoURL: https://github.com/3-Istor/demo-app
      ssoProtected: true
      hostnames:
        prod: demo-app-sandbox.3istor.com
  features:
    gatus: true
    offhoursGuard: true
```

A fuller record, exercising the Prod/Staging seam and the per-project add-ons, is in
[`examples/`](examples/).

## Imported apps

`type: service` is an app imported from a user's repository and built by the platform;
`type: backing` is a database of the platform catalogue (MongoDB, Redis, Neo4j,
Elasticsearch). Both render one `app` component, like `static`, but from different inputs:

```yaml
  apps:
    - name: user-service
      type: service
      public: false              # true gives a route on hostnames.prod
      source:                    # provenance only, Argo CD never reads it
        repo: https://github.com/acme/tinyx
        path: tinyx/user-service
        sha: 3f6c2a9d8e1b4c7a0f5e2d9c8b7a6f5e4d3c2b1a
      dependsOn:                 # apps of this project it calls
        - app: mongodb
          port: 27017            # alias defaults to the app name
    - name: mongodb
      type: backing
```

- **No `repoURL`.** The values live here, in `apps/<project>/<app>/values.yaml`, written by
  the CMP with the image digest (D1). The validator refuses a live record whose imported
  app has no values file: the CMP writes the file and the record entry in one commit (D2).
- **`dependsOn` stays in the project.** Each link becomes an ExternalName Service named
  `alias` in the app's namespace, pointing at the target's Service. The target must be a
  `service` or `backing` app of the same record; the alias cannot be the app's own name,
  `<app>-stable`, `<app>-canary`, `kubernetes`, `default`, `vault`, `keycloak`, `argocd`, and
  cannot contain dots (D18). App names and aliases start with a letter (DNS-1035: they are
  Service names). When the target's values file is here, `port` must be one of its
  `service.ports[].port`.
- **Prod only** for now, and a backing app is never public and has no `source`.

## Rules

- **The filename must match `metadata.name`.** The ApplicationSet and `cnp-clean` both
  key off the path.
- **`targetCloud` is immutable in v1.** Changing it in place does not move a project —
  it orphans everything already provisioned on the old cloud. The API rejects the update;
  editing the file by hand bypasses that check, so don't. Re-create and migrate the data.
- **`targetCloud` must name a registered Argo CD cluster.** The cluster secrets are named
  `onprem`, `aws` and `gcp` (D-03).
- **Every existing project reads back as `onprem`**, with a single `prod` environment.
  That is what the WS-1 migration backfills, and it is why the refactor is a no-op for
  them.

## Validation

`make validate`, or directly:

```bash
python3 scripts/validate-registry.py
```

It checks every record against `schema.json`, that the filename matches `metadata.name`,
that no two records claim the same name, the `dependsOn` links of imported apps, and that
each imported app of a live record has its values file. CI runs it on every push and pull request.
