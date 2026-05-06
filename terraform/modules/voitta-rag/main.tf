locals {
  # Single naming root for every resource the module creates. Keeping it
  # short matters: GKE NEG names get truncated past ~50 chars.
  prefix = var.name
}

# Required APIs. ``disable_on_destroy = false`` so a ``terraform destroy``
# of one Voitta deployment doesn't break other stuff in the customer's
# project that uses the same APIs.
resource "google_project_service" "apis" {
  for_each = toset([
    "container.googleapis.com",
    "compute.googleapis.com",
    "iam.googleapis.com",
  ])

  project            = var.project_id
  service            = each.key
  disable_on_destroy = false
}

# Reserved global IPv4 the Ingress points to. We output its address —
# the operator points an A record at it once per customer (manual step).
resource "google_compute_global_address" "ingress" {
  name    = "${local.prefix}-ingress-ip"
  project = var.project_id

  depends_on = [google_project_service.apis]
}
