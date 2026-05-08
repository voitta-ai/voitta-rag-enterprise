# Handoff: GCP Tier A deploy for voitta-rag-enterprise

**Created:** 2026-05-07
**Author:** Claude Code session (gregory.golberg + Opus 4.7)
**For:** AI Agent (next session) and the human operator
**Status:** Ready to merge + cut release + customer-test

**Delete when:** all five Next Steps below are checked off — i.e.
the 16-PR stack is merged into master, `v0.1.0` is tagged + pushed
to GHCR (and the package is public), `TF_VAR_image_tag` default is
pinned, the first customer install on a real GCP project has
verified `/healthz`, and Tier B + Tier C epics are filed. Once all
five are done, this file has no remaining audience and should be
removed in the same commit that closes out the Tier A milestone.

---

## Summary

Sixteen stacked PRs (#18-#34, on integration branch `feat/gcp-deploy`)
deliver a customer-installable Tier A deploy of the renamed
`voitta-rag-enterprise` (formerly `voitta-image-rag`) to GCP — single
GCE VM, persistent disk, IAP-only SSH, Caddy or Cloud LB for TLS,
optional CMEK, optional uptime monitoring, daily disk snapshots, plus
the bash + terraform + make plumbing the operator needs. Nothing has
been applied to a real GCP project yet. Next step is merging the
stack, cutting `v0.1.0` so the GHCR image exists, then exercising the
install on a real project.

## Project Context

- **Repo:** `voitta-ai/voitta-rag-enterprise` (was `voitta-image-rag`
  — GitHub redirects, but new GHCR path is `ghcr.io/voitta-ai/voitta-rag-enterprise`).
- **App:** filesystem-driven RAG, FastAPI + uvicorn + SQLite +
  Qdrant (embedded) + CAS on disk + watchdog + MCP. Heavy ML deps
  via `mineru[all]` (vllm, torch). See top-level `README.md`.
- **Deploy target:** customer-hosted (we don't operate it). Stage 1
  is "Tier A" — single VM. Stage 2 ("Tier B") swaps in an L4 GPU.
  Stage 3 ("Tier C") splits web/worker, swaps SQLite for Postgres,
  moves CAS to Filestore, externalizes Qdrant. Tiers B and C are
  separate epics to be filed later.
- **Local working dir:** `/Users/gregory.golberg/g/git.voitta/voitta-image-rag`
  (the directory name still says voitta-image-rag because it
  pre-dates the rename; the remote already points at the renamed
  repo).
- **Worktrees:** all per-PR worktrees live under
  `.claude/worktrees/gcp-NN-slug/`. Each is on its own
  `feat/gcp-deploy-NN-slug` branch off the previous one.

## The Plan (delivered)

A long-lived integration branch + one-PR-per-issue stack pattern.
master ← `feat/gcp-deploy` ← 01 ← 02 ← ... ← 16.

| # | PR | What | Resources/files |
|---|---|---|---|
| 01 | [#18](https://github.com/voitta-ai/voitta-rag-enterprise/pull/18) | Dockerfile + entrypoint (CPU base) | `deploy/Dockerfile`, `deploy/docker-entrypoint.sh`, `.dockerignore` |
| 02 | [#19](https://github.com/voitta-ai/voitta-rag-enterprise/pull/19) | GHCR release workflow on `v*` tag | `.github/workflows/release.yml` |
| 03 | [#20](https://github.com/voitta-ai/voitta-rag-enterprise/pull/20) | Terraform Tier A skeleton | `deploy/terraform/{main,versions,variables,network,compute,outputs}.tf` |
| 04 | [#21](https://github.com/voitta-ai/voitta-rag-enterprise/pull/21) | cloud-init + systemd | `deploy/cloud-init.yaml.tftpl`, `deploy/systemd/voitta.service` |
| 05 | [#22](https://github.com/voitta-ai/voitta-rag-enterprise/pull/22) | Caddy + Let's Encrypt (default) | `deploy/Caddyfile.tpl`, `deploy/systemd/caddy.service` |
| 06 | [#24](https://github.com/voitta-ai/voitta-rag-enterprise/pull/24) | Optional Cloud LB + managed cert | `deploy/terraform/lb.tf` |
| 07 | [#25](https://github.com/voitta-ai/voitta-rag-enterprise/pull/25) | KMS keyring + CMEK on disk | `deploy/terraform/kms.tf` |
| 08 | [#26](https://github.com/voitta-ai/voitta-rag-enterprise/pull/26) | Secret Manager + populate script | `deploy/terraform/secrets.tf`, `deploy/scripts/populate_secrets.sh`, `.env.secrets.example` |
| 09 | [#27](https://github.com/voitta-ai/voitta-rag-enterprise/pull/27) | Entrypoint resolves `sm://` refs | `deploy/docker-entrypoint.sh`, cloud-init template |
| 10 | [#28](https://github.com/voitta-ai/voitta-rag-enterprise/pull/28) | KMS envelope encryption for `folder_sync_sources` | `src/voitta_image_rag/services/crypto.py`, model + config + tests |
| 11 | [#29](https://github.com/voitta-ai/voitta-rag-enterprise/pull/29) | Daily snapshot policy | `deploy/terraform/backups.tf` |
| 12 | [#30](https://github.com/voitta-ai/voitta-rag-enterprise/pull/30) | `make deploy-*` targets | root `Makefile` |
| 13 | [#31](https://github.com/voitta-ai/voitta-rag-enterprise/pull/31) | Operational scripts | `deploy/scripts/{init,bootstrap,upgrade_image,snapshot,logs,shell}.sh`, `_lib.sh` |
| 14 | [#32](https://github.com/voitta-ai/voitta-rag-enterprise/pull/32) | Monitoring (uptime + log alerts) | `deploy/terraform/monitoring.tf` |
| 15 | [#33](https://github.com/voitta-ai/voitta-rag-enterprise/pull/33) | `docs/DEPLOY.md` install guide | `docs/DEPLOY.md` |
| 16 | [#34](https://github.com/voitta-ai/voitta-rag-enterprise/pull/34) | `deploy/README.md` quickstart + path fix | `deploy/README.md`, fix in `docs/DEPLOY.md` |

(PR #23 wasn't ours; numbering jumps from #22 to #24.)

GitHub Issues: #1-#16 with label `gcp-deploy`, milestone "Tier A —
single-VM customer deploy" (milestone #1). Each issue's body holds
the original acceptance criteria.

## Key Files

| File | Why It Matters |
|---|---|
| `deploy/terraform/.env.terraform.example` | Customer config template — every `TF_VAR_*` is documented here. |
| `.env.secrets.example` (repo root) | OAuth + session-secret template; populated values never enter terraform state. |
| `deploy/cloud-init.yaml.tftpl` | First-boot script. Conditionally renders Caddy, the systemd voitta unit (sourced from `deploy/systemd/voitta.service` via `file()` + `indent(6, ...)` plus a manual 6-space prefix), and `VOITTA_KMS_KEY` when CMEK is on. |
| `deploy/docker-entrypoint.sh` | Resolves `sm://<name>` env refs at container start via GCE metadata server. Strict-fail on errors. Pass-through when no `sm://` present (so local docker-run still works). |
| `src/voitta_image_rag/services/crypto.py` | `EncryptedString` SQLAlchemy `TypeDecorator` + `KMSEncryptor` / `PassthroughEncryptor`. Wired into `FolderSyncSource` columns `gh_pat`, `gh_token`, `gd_client_secret`, `gd_refresh_token`, `gd_service_account_json`. |
| `deploy/scripts/_lib.sh` | Shared helpers (project ID / zone / VM name / data-disk name resolvers) — bash-3.2 portable so macOS default bash works. |
| `Makefile` | `deploy-*` targets wrap terraform + the scripts. Source `.env.terraform` via `DOTENV_TF`. |
| `docs/DEPLOY.md` | Customer-facing install walkthrough. |

## Current State

**Done:**
- All 16 PRs open, each with a green local validation
  (`terraform validate`, `terraform fmt`, `bash -n`, `pytest tests/test_crypto.py`).
- Resource counts by toggle: 14 base, +2 backups (always), +5 monitoring,
  +12 LB, +4 CMEK keyring/key/2 IAM bindings, +12 Secret Manager
  (6 secrets + 6 IAM). Default toggles produce 14 + 12 + 2 = 28.
- The `voitta-image-rag` → `voitta-rag-enterprise` rename was applied
  to remotes; image references in code use the new name.

**In Progress:**
- Nothing.

**Not Started:**
- Merge the stack into master.
- Cut `v0.1.0` tag and confirm the release workflow pushes to GHCR.
- Flip the GHCR package public (one-time org-admin UI step on
  github.com → Packages → voitta-rag-enterprise → Settings →
  Change visibility).
- Customer-side `make deploy-init && deploy-plan && deploy-apply`
  on a real project.

## Decisions Made

- **Customer-installed, not SaaS.** Terraform is a customer artifact.
  No CI/CD for deploys; only the release workflow that publishes the
  image. We don't operate any infra.
- **Per-PR stack onto a long-lived `feat/gcp-deploy` integration
  branch.** Each PR's base is the previous PR's branch. Final merge
  to master is one large merge-commit (history visible) of
  `feat/gcp-deploy`. This was a deliberate trade-off; the user
  picked it explicitly mid-conversation.
- **Tier A is CPU-only.** No GPU at this stage. First-boot uses
  `VOITTA_USE_FAKE_EMBEDDERS=true` so `/healthz` answers without a
  16 GB RAM working set or downloaded weights. Real-mode is a
  one-line edit in `/etc/voitta/app.env` after secrets land.
- **Caddy is the default TLS path.** Cloud LB is opt-in via
  `TF_VAR_create_load_balancer=true`. Caddy gets ACME state on the
  data disk so disk snapshots cover certs.
- **CMEK is opt-in (`TF_VAR_enable_cmek=false` default).** Once on,
  it's effectively one-way — flipping back recreates and wipes the
  data disk (`disk_encryption_key` is force-new).
- **Direct KMS encrypt/decrypt, not envelope DEKs.** Plaintexts are
  sub-64 KiB. Envelope adds work for no win at this size/rate.
- **Migration: fresh-deploy only.** No back-fill of existing
  `folder_sync_sources` rows. Issue #10 documents this.
- **Image upgrades are out-of-band of terraform.** VM `metadata` is
  in `lifecycle.ignore_changes`. Upgrades go through
  `make deploy-upgrade` → SSH + edit `/etc/voitta/image.env` +
  restart unit.
- **Bash-3.2 portability for customer-machine scripts.**
  `populate_secrets.sh`, `_lib.sh`, all the deploy/scripts/* —
  no `mapfile`, no `declare -A`. macOS default `/bin/bash` works.
  Inside the container the entrypoint can use bash 5+.
- **No project-wide SSH keys.** VM uses `block-project-ssh-keys=TRUE`
  + `enable-oslogin=TRUE`. SSH only via IAP tunnel.
- **GHCR image: public, single visibility flip.** Workflow doesn't
  manage visibility; one-time manual step on github.com.

## Important Context

- **Git config note for the directory name vs repo name mismatch:**
  the on-disk dir is `voitta-image-rag/` but the remote points at
  `voitta-rag-enterprise.git`. GitHub redirects, but if a future
  session ever cleans up the dir name, image references in code
  must NOT change — they're already pointing at the new repo name.
- **Terraform `indent(N, str)` does NOT indent the first line.**
  When embedding the systemd unit content under
  `content: |` in the cloud-init YAML, the substitution token
  carries a manual 6-space prefix (`      ${voitta_service_indented}`)
  so `[Unit]` lines up with the rest. See `deploy/cloud-init.yaml.tftpl`.
- **`Mapped[]` annotations don't resolve in nested function scope**
  under `from __future__ import annotations`. The crypto test's
  `EncryptedString` round-trip uses SA Core (`Table` + `Column`)
  instead of ORM (`Mapped`) for that reason.
- **Rancher Desktop default VM is 8 GB.** Real-model warmup OOMs
  the container locally; the `VOITTA_USE_FAKE_EMBEDDERS=true` flag
  documented in DEPLOY.md is the standard local smoke path.
- **PR #23 is not ours.** Numbering jumps because GitHub allocated
  it to something else mid-stream.
- **CMEK + data-source caveat.** When `enable_cmek=true`, the
  terraform plan with dummy creds fails at the
  `data "google_project"` read — that's expected. Real plan with
  ADC works fine. Don't be alarmed by the dummy-creds error.

## Next Steps

1. **Merge the 16-PR stack into master.** Two ways:
   - Merge each PR into its base in order (#18 → `feat/gcp-deploy`,
     then #19 onto the moving target, etc.) — slow.
   - Merge each PR's branch directly into `feat/gcp-deploy`
     after retargeting their base. Then one merge-commit
     (`--no-ff`) of `feat/gcp-deploy` → master. **Recommended**;
     keeps per-PR history visible on master.

2. **Cut the first release.**
   ```bash
   git checkout master && git pull
   git tag v0.1.0
   git push origin v0.1.0
   ```
   Watch the release workflow in Actions. After it completes,
   flip the GHCR package to public on github.com (one-time UI
   click; the workflow can't do it).

3. **Pin the default image tag.** After the release exists,
   change `TF_VAR_image_tag` default in
   `deploy/terraform/variables.tf` from `latest` to `v0.1.0`
   (separate small PR straight onto master).

4. **First customer install on a real GCP project.**
   ```bash
   cp deploy/terraform/.env.terraform.example .env.terraform
   cp .env.secrets.example                    .env.secrets
   $EDITOR both
   gcloud auth application-default login
   make deploy-init
   make deploy-plan      # confirm resource count matches toggles
   make deploy-apply
   make deploy-secrets
   make deploy-bootstrap
   ```
   Acceptance: `curl https://<domain>/healthz` returns
   `{"ok":true}` with a real cert; SPA loads at `https://<domain>/`.

5. **File Tier B + Tier C epics.** Tier B = swap to
   `g2-standard-4` + L4 GPU + NVIDIA drivers in cloud-init. Tier C
   = port SQLite to Postgres, CAS to Filestore, Qdrant external,
   Cloud Run web/worker split. Both are weeks of work, deferred.

## Constraints

- **Don't touch app code beyond what #10 already shipped.** App
  behavior is locked at master + the crypto changes from #10.
  Tier A changes are infra-only.
- **Don't ship a back-fill migration for `folder_sync_sources`.**
  The decision was fresh-deploy only, documented in #10's PR body.
- **Don't bake `latest` into customer-facing defaults longer than
  needed.** Pin `TF_VAR_image_tag` to a real version after the
  first release tag.
- **Don't add CI for deploys.** Only the release workflow stays.
  Customer drives apply.
- **Bash-3.2 portability is mandatory for customer-machine
  scripts.** The container scripts can use bash 5+; the laptop
  scripts cannot.
- **Don't lift Tier B/C content into Tier A docs.** Pointer-only
  in DEPLOY.md.
