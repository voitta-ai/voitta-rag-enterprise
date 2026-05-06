# A dedicated VPC with a single regional subnet. We avoid the default
# network so the customer can `terraform destroy` cleanly without
# touching shared resources.

resource "google_compute_network" "vpc" {
  name                    = "${local.prefix}-vpc"
  auto_create_subnetworks = false
  routing_mode            = "REGIONAL"
}

resource "google_compute_subnetwork" "subnet" {
  name                     = "${local.prefix}-subnet"
  ip_cidr_range            = var.subnet_cidr
  region                   = var.region
  network                  = google_compute_network.vpc.id
  private_ip_google_access = true
}

# 80 + 443 from the public internet (or the operator-supplied list). The
# app itself listens on 8000; Caddy/LB in later issues fronts that.
resource "google_compute_firewall" "allow_http_https" {
  name    = "${local.prefix}-allow-http-https"
  network = google_compute_network.vpc.id

  direction     = "INGRESS"
  source_ranges = var.ingress_cidrs_http
  target_tags   = ["${local.prefix}-web"]

  allow {
    protocol = "tcp"
    ports    = ["80", "443"]
  }
}

# 22 only via IAP. No public SSH. Customer connects with
# `gcloud compute ssh <vm> --tunnel-through-iap` and never holds an SSH
# key on the VM.
resource "google_compute_firewall" "allow_iap_ssh" {
  name    = "${local.prefix}-allow-iap-ssh"
  network = google_compute_network.vpc.id

  direction     = "INGRESS"
  source_ranges = [local.iap_tcp_forwarders_cidr]
  target_tags   = ["${local.prefix}-iap"]

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }
}

# Belt-and-braces deny. GCP defaults already drop unmatched ingress, but
# an explicit low-priority deny makes audits trivial.
resource "google_compute_firewall" "deny_all_ingress" {
  name      = "${local.prefix}-deny-all-ingress"
  network   = google_compute_network.vpc.id
  direction = "INGRESS"
  priority  = 65534

  source_ranges = ["0.0.0.0/0"]

  deny {
    protocol = "all"
  }
}

# Static external IP so DNS records survive VM rebuilds.
resource "google_compute_address" "vm_ip" {
  name         = "${local.prefix}-ip"
  region       = var.region
  address_type = "EXTERNAL"
  network_tier = "PREMIUM"
}
