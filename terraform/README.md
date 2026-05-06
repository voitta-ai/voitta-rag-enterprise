# Terraform — GCP deployment

Provisions one Voitta RAG Enterprise stack per customer as a single Compute Engine VM running Container-Optimized OS. The app and an automatic-TLS reverse proxy (Caddy) run as Docker containers under systemd; a separate persistent disk holds all app state across VM lifetimes.

See [`DEPLOYMENT_LOG.md`](DEPLOYMENT_LOG.md) for the running record of what was tried and what didn't work — annotated with fixes.

## Layout

- [`modules/voitta-rag/`](modules/voitta-rag/) — the reusable module. Don't edit per-customer; pass values via the env's `main.tf`.
- [`envs/voitta-demo/`](envs/voitta-demo/) — example consumer; the live `voitta-report-builder` deployment. Copy to `envs/<customer>/` for each new deploy.

## What you need before `terraform apply`

1. **A GCP project** owned by the customer (or a sub-project under their org), with a billing account attached. Newly-created projects do **not** inherit the org's billing account; link it explicitly:
   ```bash
   gcloud billing projects link <project-id> --billing-account=<id>
   ```
2. **A GCS bucket** for the Terraform state. Created once per project; reused across re-applies:
   ```bash
   gcloud storage buckets create gs://voitta-tfstate-<short> \
     --project=<project-id> --location=us --uniform-bucket-level-access
   gcloud storage buckets update gs://voitta-tfstate-<short> --versioning
   ```
3. **Application Default Credentials** for Terraform:
   ```bash
   gcloud auth application-default login
   ```
4. **A container image tag** to deploy — produced by the [`image` GitHub Actions workflow](../.github/workflows/image.yml). The `voitta-rag-enterprise` GHCR package must be **public** for anonymous pull, or you'll need to wire `imagePullSecrets` (not currently supported by the module).
5. **OAuth credentials** (only if you want Google sign-in — for the test deploy this is skipped):
   - GCP Console → APIs & Services → Credentials → Create OAuth client ID → Web application.
   - Authorized redirect URI: `https://<your-fqdn>/api/auth/google/callback`.

## First-time apply

```bash
cd terraform/envs/<customer>
terraform init
terraform apply
```

The apply takes ~3 min. Outputs include `external_ip` — point your DNS A record at it.

## Wiring DNS + TLS

1. Create an A record at your DNS provider:
   ```
   <fqdn>  A  <external_ip>  TTL=300
   ```
2. Wait for propagation (`dig +short <fqdn>` returns the IP).
3. Set `domain = "<fqdn>"` in your env's `main.tf`.
4. Re-apply, **forcing VM replacement** so cloud-init re-runs:
   ```bash
   terraform apply -replace=module.voitta_rag.google_compute_instance.this
   ```
   Cloud-init only runs on first boot; an in-place metadata update won't reconfigure Caddy.

Caddy fetches a Let's Encrypt cert via HTTP-01 on the first inbound request, serves HTTPS on `:443`, and 308-redirects all `:80` traffic. Renewals are automatic.

## Updating the deployed image

Bump `image_uri` in the env's `main.tf` and apply with `-replace`:

```bash
terraform apply -replace=module.voitta_rag.google_compute_instance.this
```

The data PD is a separate `google_compute_disk` resource, so VM replacement preserves app state. Downtime is dominated by the new VM's image pull (~3-5 min for an in-region pull).

## Tearing down

```bash
terraform destroy
```

Wipes everything including the data PD. To preserve data across teardown, snapshot the PD first:

```bash
gcloud compute disks snapshot voitta-rag-data --zone=<zone>
```

## Cost

- VM (`c4-standard-8`, 8 vCPU / 32 GB): ~$0.40/hr
- 200 GB hyperdisk-balanced: ~$0.024/hr
- Static external IP (attached): free

≈ **$0.42/hr running**. Stop the VM (without destroying) and only the disk charges (~$17/mo for 200 GB).

## Troubleshooting

The deployment log under [`DEPLOYMENT_LOG.md`](DEPLOYMENT_LOG.md) records every gotcha hit during the first real deploy, with fixes. Highlights:

- **C4 instances reject `pd-balanced`** — the module defaults to `hyperdisk-balanced`.
- **COS host iptables drops 80/443 by default** — cloud-init opens them with explicit ACCEPT rules.
- **The 14 GB image overruns systemd's default 90s start timeout** — `voitta.service` sets `TimeoutStartSec=900`.

If a problem doesn't match any of these, SSH into the VM with `gcloud compute ssh voitta-rag-vm --tunnel-through-iap` and check `sudo journalctl -u voitta.service` and `sudo journalctl -u caddy.service`.
