# Terraform — GCP deployment

Provisions one Voitta RAG Enterprise stack per customer as a single Compute Engine VM running Container-Optimized OS. The app and an automatic-TLS reverse proxy (Caddy) run as Docker containers under systemd; a separate persistent disk holds all app state across VM lifetimes.

Two supported deployment paths:

- **[Path A — Quick bring-up (no OAuth)](#path-a--quick-bring-up-no-oauth)** — fastest end-to-end validation. The app auto-signs everyone in as a configured email via `VOITTA_DEV_USER`. Useful for: smoke-testing the cluster + image + DNS + cert, internal demos, environments behind a separate auth proxy.
- **[Path B — Production (Google OAuth)](#path-b--production-google-oauth)** — real Google "Sign in with Google" flow. Each user authenticates with their Google account; admins manage who can sign in via the in-app **🔒 Admin** panel. This is the customer-facing path.

You can deploy via Path A first to validate plumbing, then switch to Path B by adding OAuth credentials and recreating the VM.

See [`DEPLOYMENT_LOG.md`](DEPLOYMENT_LOG.md) for the running record of what was tried and what didn't work — annotated with fixes.

## Layout

- [`modules/voitta-rag/`](modules/voitta-rag/) — the reusable module. Don't edit per-customer; pass values via the env's `main.tf`.
- [`envs/voitta-demo/`](envs/voitta-demo/) — example consumer; the live `voitta-report-builder` deployment. Copy to `envs/<customer>/` for each new deploy.

---

## Common pre-flight (both paths)

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

4. **A container image tag** to deploy — produced by the [`image` GitHub Actions workflow](../.github/workflows/image.yml) on every push to `master`. The `voitta-rag-enterprise` GHCR package must be **public** for anonymous pull (one-time toggle in GitHub package settings).

5. **A working DNS subdomain** you can point at the VM's external IP. (You can apply once without it; the module outputs the IP, you wire DNS, then re-apply with `var.domain` set to enable HTTPS.)

---

## Path A — Quick bring-up (no OAuth)

Use this when you want the deployment running end-to-end in ~15 min with the smallest moving-parts count. Anyone visiting the URL gets auto-signed-in as the configured `VOITTA_DEV_USER` email — there is no real per-user authentication. **Do not expose this to the public internet for actual use.**

### env's `main.tf`

```hcl
module "voitta_rag" {
  source = "../../modules/voitta-rag"

  project_id = "your-project"
  region     = "us-central1"
  zone       = "us-central1-a"

  name      = "voitta-rag"
  image_uri = "ghcr.io/voitta-ai/voitta-rag-enterprise:sha-XXXXXXX"

  domain = "rag.example.com"   # leave "" for plain HTTP during bring-up

  super_admins = ["you@example.com"]

  # NO OAuth. Auto-sign-in as this email.
  google_oauth_client_id     = ""
  google_oauth_client_secret = ""

  extra_env = {
    VOITTA_DEV_USER = "you@example.com"
  }
}
```

### Apply

```bash
cd terraform/envs/<customer>
terraform init
terraform apply
```

Note the `external_ip` output, point a DNS A record at it, then re-apply with `domain = "<fqdn>"`:

```bash
terraform apply -replace=module.voitta_rag.google_compute_instance.this
```

Within ~10 min you can open `https://<fqdn>/` and you'll already be signed in as the dev-user.

### Switching from Path A to Path B later

Replace the `extra_env` block with real OAuth credentials (see Path B below) and:

```bash
terraform apply -replace=module.voitta_rag.google_compute_instance.this
```

Cloud-init only runs on first boot, so VM replacement is required. The data PD is a separate resource and survives.

---

## Path B — Production (Google OAuth)

This is the path real customer deployments use. Users sign in with their Google account; admins manage who is allowed in via the in-app panel.

### How login credentials are delivered

There are **no per-customer credentials we ship** — every user authenticates with their own Google account through Google's OAuth 2.0 flow. The deployment only needs:

- An **OAuth client** (Client ID + Client Secret) registered in the customer's GCP project. This identifies the deployment to Google but is not user credentials.
- A **list of who is allowed in**, managed by admins inside the app (no redeploy needed).

```
┌───────────────┐     ┌───────────────┐     ┌─────────────────────┐
│   end user    │────▶│  Google OAuth │────▶│ Voitta callback URL │
│  (browser)    │     │  consent      │     │  /api/auth/google/  │
│               │     │  screen       │     │     callback        │
└───────────────┘     └───────────────┘     └─────────────────────┘
                                                      │
                                                      ▼
                                       ┌──────────────────────────┐
                                       │ App checks the verified  │
                                       │ email against:           │
                                       │   • super_admins (env)   │
                                       │   • allowed_domains.txt  │
                                       │   • allowed_users.txt    │
                                       │   • blocked_users.txt    │
                                       │ Allow → set session.     │
                                       │ Deny → 403.              │
                                       └──────────────────────────┘
```

The app **never sees a password**. Google verifies the user's identity, returns a token, the app reads the verified email from it, and the allowlist gate decides admit-or-deny.

### Step 1 — register the OAuth client (manual, browser only)

Google does **not** expose a public API or CLI for creating "Sign in with Google" OAuth clients with custom redirect URIs. (`gcloud iam oauth-clients` and the Terraform `google_iap_client` resource are for Workforce Identity / IAP, which is a different product.) This step is unavoidably manual, ~5 min in a browser:

1. Open `https://console.cloud.google.com/auth/overview?project=<project-id>` and create the consent screen:
   - **User Type**: External (Internal requires a Google Workspace org)
   - **App name**, support email, developer email — all required
   - **Authorized domains**: your subdomain's parent (e.g. `voitta.ai`)
   - Under **Test users**, add the email addresses you want to be able to sign in with — until you publish the app, only listed test users complete the OAuth dance. (Or click **Publish app** at the top — for the scopes we use, `email`, `profile`, `openid`, no Google verification is required.)

2. Go to **APIs & Services → Credentials → + Create Credentials → OAuth client ID**:
   - **Application type**: Web application
   - **Authorized JavaScript origin**: `https://<your-fqdn>`
   - **Authorized redirect URI**: `https://<your-fqdn>/api/auth/google/callback`
   - Click **Create**

3. Copy the **Client ID** (`...apps.googleusercontent.com`) and **Client secret** (`GOCSPX-...`).

### Step 2 — write to `terraform.tfvars`

`terraform.tfvars` is **gitignored** at the repo root, so the secret never lands in source:

```hcl
# terraform/envs/<customer>/terraform.tfvars
google_oauth_client_id     = "...apps.googleusercontent.com"
google_oauth_client_secret = "GOCSPX-..."
```

### Step 3 — env's `main.tf`

```hcl
variable "google_oauth_client_id"     { type = string, sensitive = true }
variable "google_oauth_client_secret" { type = string, sensitive = true }

module "voitta_rag" {
  source = "../../modules/voitta-rag"

  project_id = "your-project"
  region     = "us-central1"
  zone       = "us-central1-a"

  name      = "voitta-rag"
  image_uri = "ghcr.io/voitta-ai/voitta-rag-enterprise:sha-XXXXXXX"

  domain = "rag.example.com"

  # Bootstrap super-admins. ALWAYS admitted at sign-in (block-list
  # aside) and stamped is_admin=true on every login. The recovery path
  # if allowlists get wiped or every admin gets demoted.
  super_admins = ["you@example.com"]

  google_oauth_client_id     = var.google_oauth_client_id
  google_oauth_client_secret = var.google_oauth_client_secret

  # No extra_env here — VOITTA_DEV_USER would short-circuit the OAuth path.
}
```

### Step 4 — apply

```bash
cd terraform/envs/<customer>
terraform init
terraform apply
```

Note the `external_ip` output, wire DNS, then re-apply with `domain = "<fqdn>"`:

```bash
terraform apply -replace=module.voitta_rag.google_compute_instance.this
```

### Step 5 — first sign-in + admin handoff

1. Open `https://<your-fqdn>/` — you'll see the **Sign in with Google** button.
2. Sign in with one of the addresses listed in `super_admins`. The app stamps `is_admin = true` on the User row on every super-admin login (so a demoted-then-re-logged-in super-admin is automatically re-promoted).
3. Click the **🔒 Admin** button in the top-right.
4. Add **Allowed domains** for everyone you want to admit (e.g. `customer.com`).
5. Use the **Users** section's Add row to admit individual addresses outside those domains, with optional admin grant.
6. Use **Blocked** to revoke specific addresses (trumps everything, including super-admins).
7. Use the **View as** button on any user row to debug their permissions live.

The three allowlist files (`allowed_domains.txt`, `allowed_users.txt`, `blocked_users.txt`) live under `<data_dir>/admin/` on the data PD, and are also human-editable via SSH for emergency lockout recovery.

### Switching from Path B back to Path A

Set `google_oauth_client_id = ""` and add a `VOITTA_DEV_USER` to `extra_env`, then re-apply with `-replace=...`. The OAuth client in the GCP console can be left in place — empty credentials in the env disable Google sign-in.

---

## Wiring DNS + TLS (both paths)

1. Note the `external_ip` output from `terraform apply`.
2. Create an A record at your DNS provider:
   ```
   <fqdn>  A  <external_ip>  TTL=300
   ```
3. Wait for propagation (`dig +short <fqdn>` returns the IP).
4. Set `domain = "<fqdn>"` in your env's `main.tf`.
5. Re-apply, **forcing VM replacement** so cloud-init re-runs:
   ```bash
   terraform apply -replace=module.voitta_rag.google_compute_instance.this
   ```

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
- **Cloud-init only runs on first boot** — switching paths (A↔B) or changing TLS state requires `terraform apply -replace=...google_compute_instance.this`.
- **"Access denied" from Google during sign-in** — your email isn't on the OAuth consent screen's Test users list. Add it, or publish the app.

If a problem doesn't match any of these, SSH into the VM with `gcloud compute ssh voitta-rag-vm --tunnel-through-iap` and check `sudo journalctl -u voitta.service` and `sudo journalctl -u caddy.service`.
