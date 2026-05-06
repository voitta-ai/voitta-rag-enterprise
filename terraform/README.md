# Terraform — GCP deployment

Provisions one Voitta RAG Enterprise stack per customer on GKE Standard.

## Layout

- [`modules/voitta-rag/`](modules/voitta-rag/) — the reusable module. Don't edit per-customer; pass values in via tfvars.
- [`envs/example/`](envs/example/) — copy this to `envs/<customer>/` for each new deployment. Each env has its own state bucket.

## What you need before `terraform apply`

1. **A GCP project** owned by the customer (or a sub-project under their org). You — the operator — need `roles/owner` or equivalent.
2. **A GCS bucket** for the Terraform state. Create it out of band (see `backend.tf.example`).
3. **OAuth credentials**: in the customer's project, GCP Console → APIs & Services → Credentials → Create OAuth client ID → Web application. The redirect URI is `https://<host>/api/auth/google/callback`; you'll know the host *after* the first apply, so just register a placeholder for now and update it once DNS is wired.
4. **A container image tag** to deploy — produced by the `image` GitHub Actions workflow.

## First-time apply

```bash
cd terraform/envs/example       # or your customer copy
cp terraform.tfvars.example terraform.tfvars
cp backend.tf.example backend.tf
$EDITOR terraform.tfvars        # fill in project_id, name, OAuth, allowed_domains, image_uri

terraform init
terraform apply
```

The first apply is sometimes flaky because the kubernetes provider tries to authenticate against a cluster that doesn't exist yet. If you hit that, run:

```bash
terraform apply -target=module.voitta_rag.google_container_node_pool.primary
terraform apply
```

## After apply — manual one-time wiring per customer

`terraform output` prints the four values you need:

```
ingress_ip   = "34.117.x.y"
redirect_uri = "https://<host>/api/auth/google/callback"
cluster_name = "acme-rag-cluster"
namespace    = "acme-rag"
```

1. **DNS**: point an A record `rag.customer.com` at `ingress_ip`. Wait a few minutes for propagation.

2. **Managed certificate**: GCE Ingress can do TLS via a `ManagedCertificate` CRD. Apply it directly with `kubectl`:

   ```bash
   gcloud container clusters get-credentials "$(terraform output -raw cluster_name)" \
     --zone us-central1-a

   kubectl -n "$(terraform output -raw namespace)" apply -f - <<EOF
   apiVersion: networking.gke.io/v1
   kind: ManagedCertificate
   metadata:
     name: voitta-rag-cert
   spec:
     domains:
       - rag.customer.com
   EOF

   kubectl -n "$(terraform output -raw namespace)" annotate ingress voitta-rag \
     networking.gke.io/managed-certificates=voitta-rag-cert --overwrite
   ```

   Cert provisioning takes 15–60 min. Status:

   ```bash
   kubectl -n "$(terraform output -raw namespace)" get managedcertificate voitta-rag-cert
   ```

3. **Update the OAuth client** in the customer's GCP Console with the real redirect URI: `https://rag.customer.com/api/auth/google/callback`.

4. **Smoke test**: open `https://rag.customer.com/` in a browser, click Sign in with Google, confirm an in-domain email is admitted and an out-of-domain one gets a 403.

## Updating a deployment

```bash
$EDITOR terraform.tfvars        # bump image_uri to the new tag
terraform apply
```

The single replica recreates with ~30–60s of downtime — `strategy: Recreate` is required because SQLite + the embedded Qdrant cannot run two writers against the same PD.

## Tearing down

```bash
terraform destroy
```

The PD is deleted along with everything else. **There are no automatic backups.** If you need to preserve the data, snapshot the PD before destroy:

```bash
gcloud compute disks snapshot \
  $(gcloud compute disks list --filter='name~voitta-rag-data' --format='value(name)') \
  --zone us-central1-a
```
