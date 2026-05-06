# `voitta-rag` Terraform module

Provisions a single-replica Voitta RAG Enterprise deployment on GKE Standard.

## What it creates

- A GKE Standard cluster with one C-family node (default `c4-standard-8`).
- A 200 GB balanced PD mounted at `/data` for SQLite, CAS, embedded Qdrant, uploads, and the Drive mirror.
- A Deployment with `replicas: 1, strategy: Recreate` — required because SQLite + embedded Qdrant cannot run two writers.
- A NodePort Service + GCE Ingress with a reserved global static IP.
- A ConfigMap rendered from `extra_users` and mounted as `users.txt`.
- A Kubernetes Secret carrying `VOITTA_GOOGLE_AUTH_CLIENT_*` and `VOITTA_SESSION_SECRET`.

## What it does NOT do

- DNS records — `ingress_ip` is output; the operator points a customer A record at it.
- Managed TLS cert — provision a `ManagedCertificate` and update the Ingress annotation manually after DNS propagates. (See `terraform/README.md` at the repo root for the one-liner.)
- Backups — out of scope for v1.
- Multi-tenancy — one stack per customer. If you need multiple customers, run the module multiple times with different `var.name` and tfvars files.

## Inputs

See [`variables.tf`](variables.tf). The non-obvious ones:

- `allowed_domains` + `extra_users` are **both** consulted at the OAuth callback. Empty + empty denies every sign-in.
- `image_uri` should pin a tag (`:v0.1.0`), not `:latest`, so a `terraform apply` is the only thing that rolls the deployment.
- `machine_type` defaults to `c4-standard-8` — if C4 isn't in your region, drop to `c3-standard-8` (also has AMX) or `n2-standard-8`.

## Outputs

- `ingress_ip` — point an A record here.
- `redirect_uri` — register on the OAuth client (replace `<host>`).
- `cluster_name`, `namespace` — for direct `kubectl` access.
