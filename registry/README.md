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
and that no two records claim the same name. CI runs it on every push and pull request.
