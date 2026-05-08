# Deploying voitta-rag-enterprise on GCP (Tier A)

This walkthrough takes a customer from "I have a GCP project" to "the
app is serving on my domain" on a single Compute Engine VM. End state:
one `e2-standard-4` VM, persistent data disk, IAP-only SSH, Caddy or
Cloud LB doing TLS, optional CMEK, optional uptime monitoring, daily
disk snapshots.

The Tier B (GPU) and Tier C (Postgres + Filestore + Cloud Run split)
upgrades live in their own epics; this guide is Tier A only.

## Prerequisites

- A GCP project (not the one you also use for personal projects).
- Billing enabled.
- Owner role on the project for the operator running the install.
- A domain name you can point at a static IP (e.g. `voitta.example.com`).
- Local tooling:
  - `gcloud` CLI, logged in.
  - `terraform >= 1.6`.
  - `bash`, `make`, `git`.

Optional but recommended: a Slack or PagerDuty integration if you
plan to enable monitoring. Tier A wires email only; layer your own
on top of the email destination.

## Cost (rough monthly, us-central1)

| Item | Cost |
|---|---|
| `e2-standard-4` (4 vCPU / 16 GB), sustained | ~$98 |
| `pd-balanced` 200 GB | ~$24 |
| Static external IP (in use) | ~$3 |
| Daily snapshots, 7-day retention, ~30 GB used | ~$5 |
| Cloud LB (only if `create_load_balancer=true`) | ~$25 |
| Managed SSL cert | $0 |
| KMS key + ops (only if `enable_cmek=true`) | <$1 |
| Secret Manager (6 secrets, light use) | <$1 |
| Cloud Logging + Monitoring (light use) | <$5 |
| **Total** | **~$130-160 / mo** without LB; **~$160-190** with |

GPU costs land in Tier B and are NOT included.

## One-time setup

```bash
git clone git@github.com:voitta-ai/voitta-rag-enterprise.git
cd voitta-rag-enterprise

cp deploy/terraform/.env.terraform.example .env.terraform
$EDITOR .env.terraform
# fill in:
#   TF_VAR_project_id=<your project>
#   TF_VAR_domain=voitta.example.com
#   (optionally) TF_VAR_create_load_balancer=true
#   (optionally) TF_VAR_enable_cmek=true
#   (optionally) TF_VAR_enable_monitoring=true + alert_email

cp .env.secrets.example .env.secrets
$EDITOR .env.secrets
# fill in OAuth client + session secret. Leave the rest blank for
# fake/single-user first-boot if you just want to verify health.

gcloud auth application-default login
make deploy-init
```

## Deploy

```bash
make deploy-plan      # review the plan; make sure resource counts
                      # match the toggles you flipped (14 default,
                      # +5 monitoring, +12 LB, +2 backups, etc.)

make deploy-apply     # ~5-10 min on a fresh project
```

Outputs you'll want:

```
$ cd deploy/terraform && terraform output
vm_external_ip          = "1.2.3.4"
lb_external_ip          = null      # or the LB IP if you toggled it
ssh_command             = "gcloud compute ssh ..."
secret_ids              = [...]
manual_snapshot_command = "..."
```

## DNS

Before TLS works, point your domain at the VM (or LB) IP:

```
A    voitta.example.com    <vm_external_ip or lb_external_ip>
```

- **Caddy path** (`create_load_balancer=false`, default): Let's
  Encrypt issues immediately once DNS resolves. If DNS isn't ready
  Caddy serves a self-signed cert until the next ACME retry — wait
  ~5 min after the A record propagates.
- **Cloud LB path** (`create_load_balancer=true`): Google-managed
  cert provisioning waits on DNS to validate; expect 15-60 min after
  `terraform apply` even with DNS already pointing at the LB.

## Secrets

```bash
make deploy-secrets       # adds a new version of every non-empty
                          # entry in .env.secrets
```

Secrets are stored in Secret Manager; the VM's service account has
`secretAccessor` on each. Container picks them up at boot via the
`sm://` resolver in the entrypoint.

To enable real OAuth + signed sessions (off by default for first
boot), SSH in and edit the runtime config:

```bash
make deploy-shell
sudo $EDITOR /etc/voitta/app.env
# - Drop  VOITTA_USE_FAKE_EMBEDDERS=true
# - Drop  VOITTA_SINGLE_USER=true
# - Uncomment the VOITTA_*=sm://... lines
sudo systemctl restart voitta
exit

make deploy-logs          # confirm "resolved KEY from sm://NAME"
                          # lines, then "Application startup complete."
```

## Verify

```bash
make deploy-bootstrap     # SSH + curl localhost:8000/healthz
curl https://voitta.example.com/healthz   # external + TLS
```

Visit `https://voitta.example.com/` for the SPA.

## Operations

| Task | Command |
|---|---|
| Tail app logs | `make deploy-logs` |
| Tail Caddy logs | `bash deploy/scripts/logs.sh caddy` |
| SSH in | `make deploy-shell` |
| Roll forward to a new image tag | `make deploy-upgrade` (or `bash deploy/scripts/upgrade_image.sh v0.2.0`) |
| Ad-hoc snapshot before risky ops | `make deploy-backup-now` |
| Rotate a secret | edit `.env.secrets`, re-run `make deploy-secrets`, restart unit |

## Troubleshooting

### `gcloud compute ssh` hangs / times out
Likely IAP IAM. Operator's identity needs `roles/iap.tunnelResourceAccessor`
+ `roles/compute.osLogin` (or be a project Owner). Granted in the
console under IAM & Admin.

### `terraform apply` errors with "could not find policy ... compute service agent"
The Compute Engine service agent gets created lazily on first VM
spin-up. Re-running `terraform apply` once the agent exists clears
this; CMEK key bindings depend on it.

### Caddy serves a self-signed cert
DNS hasn't propagated yet. Caddy retries ACME on a backoff. Confirm
with `make deploy-logs` — pointing at `caddy` via
`bash deploy/scripts/logs.sh caddy`.

### Cloud LB managed cert stays `PROVISIONING` forever
DNS for `var.domain` (or any alias in `var.domain_aliases`) doesn't
resolve to the LB IP. Fix DNS, then wait — Google retries
automatically every ~10 min.

### `/healthz` returns 200 but `/` is broken
The app boots without OAuth in fake-mode. Login flow needs the real
Sign-in-with-Google credentials populated and `VOITTA_GOOGLE_AUTH_*`
uncommented (see Secrets above).

### Container won't start, logs show "could not resolve KEY=sm://..."
Either the secret has no version (run `make deploy-secrets` after
filling in `.env.secrets`) or the VM service account lacks
`secretAccessor` on it (terraform handles that — re-run
`make deploy-apply`).

## Backup + restore

Auto-snapshots run daily at 03:00 UTC, retained 7 days by default.

To restore from a snapshot:

```bash
SNAP=voitta-rag-data-2026-05-08-04-00
gcloud compute disks create voitta-rag-data-restore \
    --source-snapshot=$SNAP \
    --zone=us-central1-a \
    --project=$TF_VAR_project_id

# Stop the unit, swap the disk, start it again.
make deploy-shell
sudo systemctl stop voitta
exit

gcloud compute instances detach-disk voitta-rag-vm \
    --disk=voitta-rag-data --zone=us-central1-a
gcloud compute instances attach-disk voitta-rag-vm \
    --disk=voitta-rag-data-restore --device-name=voitta-data \
    --zone=us-central1-a

make deploy-shell
sudo mount -a && sudo systemctl start voitta
```

## Rotation

| Secret | How |
|---|---|
| KMS key | `gcloud kms keys versions create ...`; auto-rotates every 90 days. New writes use the new version; old ciphertext stays readable. |
| OAuth client secret | edit `.env.secrets`, `make deploy-secrets`, restart unit |
| Session HMAC | same as OAuth. Rotation logs out everyone — that's the point. |
| `gh_pat` / `gd_*` per-folder secrets | Done via the SPA's folder edit UI; the values are encrypted via `services/crypto.py` before going to SQLite. |

## Going beyond Tier A

- **Tier B** swaps the VM for `g2-standard-4` with an L4 GPU and
  reorganizes the systemd unit for GPU drivers. See the Tier B epic
  when it lands.
- **Tier C** splits web/worker, ports SQLite to Cloud SQL Postgres,
  CAS to Filestore, Qdrant to Qdrant Cloud or self-hosted on GKE.
  Larger lift; only worth it past 99.9% SLA.

## Destroying

```bash
make deploy-destroy
```

Asks for `y/N` confirmation. Snapshots are retained
(`on_source_disk_delete=KEEP_AUTO_SNAPSHOTS`); delete them
explicitly via `gcloud compute snapshots delete` if you also want
those gone.
