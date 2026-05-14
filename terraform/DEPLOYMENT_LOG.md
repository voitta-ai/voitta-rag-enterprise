# GCP deployment log — first real apply

Running ledger of what was attempted, what worked, what didn't.
Append-only — when something fails and gets fixed, leave both entries
in so the next operator sees the gotcha.

## Goal

Stand up the Voitta RAG Enterprise stack in `voitta-report-builder`
(GCP project number `300344106100`) end-to-end:

- GKE Standard cluster with one C-family node
- Image pulled from a public registry
- Real Google OAuth gating sign-ins, allowlisted to a chosen domain
- Browser-reachable at `https://<subdomain>.<domain>` with a valid cert

Test budget: ~$2 of compute. Same-day teardown.

## Phase 0 — pre-flight

### Decisions

- Target project: **`voitta-report-builder`** (active, created 2026-05-04)
- Auth account: **`roman.semein@gmail.com`** (already authenticated in `gcloud`)
- Region / zone: **`us-central1` / `us-central1-a`** (C4 instances available)
- Machine type: **`c4-standard-8`** (default)

### Pending decisions (blocking)

- [ ] **Subdomain to use** — operator needs to pick one and confirm they can add an A record at the registrar.
- [ ] **Image registry** — GHCR (public, no GCP setup) vs Artifact Registry inside `voitta-report-builder` (more isolation, more setup).

## Phase 1 — GCP pre-flight ✅

### What worked

```bash
gcloud config set project voitta-report-builder
gcloud services enable \
    container.googleapis.com compute.googleapis.com iam.googleapis.com
gcloud storage buckets create gs://voitta-tfstate-report-builder \
    --project=voitta-report-builder \
    --location=us \
    --uniform-bucket-level-access
gcloud storage buckets update gs://voitta-tfstate-report-builder --versioning
```

### What didn't, the first time

- **Project had no billing account**. First `services enable` returned
  `UREQ_PROJECT_BILLING_NOT_FOUND`. Linked the open billing account
  with:
  ```bash
  gcloud billing projects link voitta-report-builder \
      --billing-account=01E304-ED58D9-748E41
  ```
  Then `services enable` succeeded.
- **Subtle**: a freshly-created GCP project does NOT inherit the org's
  billing account. The console UI normally prompts for it; `gcloud
  projects create` doesn't, and it bites you the first time you try to
  enable any chargeable API.

### Decisions confirmed

- Subdomain: **`rag-enterprise-demo.voitta.ai`**
- Image registry: **GHCR** via the existing GitHub Actions workflow.
  Repo `voitta-ai/voitta-rag-enterprise`. Workflow currently only
  triggered on `main`; added `master` to its trigger list and pushed.
  CI run ID `25411177243` queued, ~30-40 min to complete.

## Phase 2 — image build + push

### Attempt 1: failed at 22min — out of disk on GHA runner

```
System.IO.IOException: No space left on device
```

The image's warm stage produces ~12GB of HF cache, and the multi-stage
copy temporarily doubles disk use to ~35GB peak. The hand-rolled `rm
-rf` "Free disk" step in the workflow only freed ~25GB.

### Fix

Replaced with `jlumbroso/free-disk-space@main` which reliably reclaims
~45GB by also dropping haskell, large-packages, and swap.

### Attempt 2: in flight (run id 25411995245)

## Phase 3 — Terraform apply

### Setup

- Installed Terraform 1.15.1 via `brew install hashicorp/tap/terraform`.
- Created `terraform/envs/voitta-demo/` (env dir) + `terraform.tfvars`
  pointing at `image_uri = ghcr.io/voitta-ai/voitta-rag-enterprise:sha-7d03e12`.
- Set up ADC: `gcloud auth application-default login`. Until this runs
  the GCS backend init fails with `could not find default credentials`.

### Attempt 1 — cycle error at plan time

Single-stack design failed:

```
Error: Cycle:
  module.voitta_rag.kubernetes_namespace.this,
  ...
  data.google_container_cluster.voitta,
  provider["registry.terraform.io/hashicorp/kubernetes"],
  ...
```

This is the canonical "kubernetes provider config depends on the cluster
created in the same state file" problem — Hashicorp's provider docs
explicitly say not to do this. The kubernetes provider's `host` /
`cluster_ca_certificate` are computed from the GKE cluster, but the
provider is also used by resources in the same module, creating a cycle
that Terraform's plan refuses to resolve.

### Attempt 2 — split into cluster + workload stacks

Restructured to two-stack pattern. Cluster apply succeeded but was
**aborted mid-flight** for the next reason.

### Pivot away from GKE entirely

Operator (correctly) pushed back: for a single-replica stateful
workload, GKE is overkill. The k8s control plane fee is ~$73/mo, the
PVC abstraction creates a PD anyway, and the Ingress creates an LB we
don't need for one container.

**Going with Compute Engine VM + COS instead.**

### Cleanup

The cluster apply got past the cluster creation stage before SIGINT
took effect — cluster + node pool existed on GCP, IP existed, but
Terraform's state was empty (write happens on apply success only).
Deleted via gcloud:

```bash
gcloud compute addresses delete voitta-rag-ingress-ip --global --quiet
# Cluster is mid-PROVISIONING, can't delete yet — wait + retry:
until ! gcloud container clusters describe voitta-rag-cluster \
    --zone=us-central1-a --format='value(status)' 2>/dev/null | grep -q PROVISIONING; do
  sleep 15
done
gcloud container clusters delete voitta-rag-cluster \
    --zone=us-central1-a --quiet
```

### Lesson

If you `^C` during a long-running create that's already round-tripped
to the cloud API, the resource exists in the cloud but not in Terraform
state. Always reach for `gcloud` to clean up, not `terraform destroy`.

### Attempt 3 — CE VM with COS + Caddy ✅

New module: `modules/voitta-rag/` (single-stack, no cycles). Resources:

- `google_compute_address.this` (static external IP)
- `google_compute_disk.data` (200 GB hyperdisk-balanced for app state)
- `google_compute_firewall.http` (80/443)
- `google_compute_instance.this` (`c4-standard-8`, COS, attached PD,
  cloud-init bootstraps mount + voitta + caddy as systemd units)

Total: ~250 lines of HCL vs. ~600 for the GKE version.

Cost while running: ~$0.42/hr. Stop the VM and only the disk charges
(~$17/mo for 200GB).

#### Gotchas hit, fixed

1. **C4 boot disk needs hyperdisk-balanced**, not pd-balanced. The 400
   from the API was the only signal. Same applies to attached data
   disks. Default flipped to `hyperdisk-balanced`.
2. **`docker pull` of a 14GB image overruns systemd's default
   `TimeoutStartSec=90s`**. The unit ends up in restart-loop, but every
   restart resumes the pull from cache. Set `TimeoutStartSec=900` to
   avoid the wasted churn.
3. **COS host iptables defaults to `INPUT DROP` with only port 22
   accepted.** GCP's VPC firewall opens 80/443 fine but the in-VM
   chain silently drops them. Added explicit `iptables -I INPUT -p
   tcp --dport 80/443 -j ACCEPT` to cloud-init `runcmd`.

#### TLS

- `var.domain` controls Caddyfile rendering.
- Empty `domain` → `:80 { reverse_proxy 127.0.0.1:8000 }` (plain HTTP,
  for bring-up before DNS).
- Set `domain` → `${domain} { reverse_proxy 127.0.0.1:8000 }` — Caddy
  fetches a Let's Encrypt cert via HTTP-01 on first request, serves
  HTTPS on 443, 308-redirects all HTTP to HTTPS, renews before expiry.
- Port 80 stays open: required for ACME renewals. Only ever serves the
  redirect (or ACME challenge responses).

#### Switching from HTTP-only to TLS

Set `domain` in tfvars and apply with `-replace=...google_compute_instance.this`.
Cloud-init only runs on first boot, so an in-place metadata update
won't reconfigure Caddy — VM must be recreated. Data PD persists via
the separate `google_compute_disk.data` resource. ~10 min downtime
because the new VM re-pulls the 14GB image (no shared docker cache
across VM lifetimes — could add later via a Cloud Storage layer cache
if pull time becomes painful).

## Phase 4 — DNS + ManagedCertificate (not yet run)

## Phase 5 — OAuth client wiring (not yet run)

## Phase 6 — end-to-end smoke (not yet run)

## Phase 7 — teardown (not yet run)

## Phase 5 — admin panel + impersonation (in-app)

Replaced the env-var allowlist (`VOITTA_ALLOWED_DOMAINS` + `users.txt`)
with a runtime-mutable surface persisted on the data PD:

- `<data_dir>/admin/allowed_domains.txt`
- `<data_dir>/admin/allowed_users.txt`
- `<data_dir>/admin/blocked_users.txt`

Editable from the **🔒 Admin** panel in the SPA. Block-list trumps
allow. New `users.is_admin` column with a forward-only ALTER TABLE in
`init_db` (no Alembic). `VOITTA_SUPER_ADMINS` env (Terraform var
`super_admins`) is the bootstrap-admin recovery path — always admitted,
always re-stamps is_admin on every sign-in.

`/api/admin/*` — full surface for allowlist editing, blocklist editing,
listing users, toggling admin, pre-creating + admin-granting users who
haven't signed in yet, and impersonation.

Impersonation is wired through the `current_user` FastAPI dependency:
admins can choose "View as <other>" from the panel; ACL/visibility code
downstream uses the impersonated user's permissions transparently.
Admin checks always go through a separate `real_user` dependency so
impersonation never confers admin rights.

### Gotchas

- **Non-trivial CSS bug**: `.admin-list` had `flex-shrink: 1` (the
  default) inside a flex-column modal-body with `overflow-y: auto`.
  Items rendered at correct height (36px each) but the *parent ul*
  collapsed to 2px under the column flex layout — overflow:auto isn't
  enough on its own. Fix: `flex-shrink: 0` on `.admin-list`.
- "Allowed users" section was redundant once the Users table grew an
  Add row that wraps allowlist+admin grant in a single click. Dropped
  it; the panel is 2-up (Allowed domains | Blocked) on top, Users
  below.
- The default `<button>` next to a `class=""` rendered as a stark
  white-on-black close button. Other modals already use `btn-text`;
  matched it.

## Phase 6 — Google OAuth wired (in flight at time of writing)

Manual steps in the GCP console (no CLI / Terraform / API path exists
for "Sign in with Google" OAuth clients with custom redirect URIs —
`gcloud iam oauth-clients` is for IAP/Workforce only):

1. Create consent screen at
   `console.cloud.google.com/auth/overview?project=<id>` (External,
   add test users).
2. Create OAuth client at `console.cloud.google.com/apis/credentials`
   (Web application; redirect URI =
   `https://<fqdn>/api/auth/google/callback`).
3. Paste Client ID + secret into the env's `terraform.tfvars`.
4. `terraform apply -replace=module.voitta_rag.google_compute_instance.this`.

## Lessons learned

- **Always reach for the smallest viable shape first.** Started this
  attempt with GKE because it sounded "right" for a server-side app;
  burned a couple hours on the GKE → Compute Engine pivot. CE VM was
  the right answer from the start.
- **Verify cloud-init re-runs on metadata change**: it does not.
  `terraform apply -replace=...google_compute_instance.this` is the
  only way to roll the VM after a cloud-init template change. Documented
  in `terraform/README.md`.
- **The `gcloud auth login` vs `gcloud auth application-default login`
  distinction bites the first time you hit it.** Terraform's GCS
  backend needs ADC, not the regular user creds.
- **Forward-only schema migrations are fine until they aren't.** The
  current `_ensure_column` helper handles ADD COLUMN cases; anything
  more complex (drop, rename, type change) will need Alembic. Leaving
  that as a future-Roman problem.
