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

## Phase 1 — GCP pre-flight (not yet run)

Planned commands:

```bash
gcloud config set project voitta-report-builder
gcloud services enable \
  container.googleapis.com \
  compute.googleapis.com \
  iam.googleapis.com \
  artifactregistry.googleapis.com   # only if we use AR

gsutil mb -p voitta-report-builder -l us -b on \
  gs://voitta-tfstate-report-builder
gsutil versioning set on gs://voitta-tfstate-report-builder
```

## Phase 2 — image build + push (not yet run)

## Phase 3 — Terraform apply (not yet run)

## Phase 4 — DNS + ManagedCertificate (not yet run)

## Phase 5 — OAuth client wiring (not yet run)

## Phase 6 — end-to-end smoke (not yet run)

## Phase 7 — teardown (not yet run)

## Lessons learned

(populated as we go)
