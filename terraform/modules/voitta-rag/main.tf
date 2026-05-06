# Voitta RAG Enterprise — single-VM deployment.
#
# Shape:
#   • One Compute Engine VM running Container-Optimized OS
#   • One persistent disk attached at /mnt/disks/voitta (formatted ext4
#     on first boot, mounted persistently)
#   • The Voitta container runs from cloud-init under containerd
#   • Caddy runs alongside as the TLS terminator + reverse proxy. It
#     gets a free Let's Encrypt cert for the configured FQDN as soon as
#     DNS resolves to the VM's static IP.
#   • Static external IP attached to the VM. Operator points DNS at the
#     IP output by this module.
#
# Deliberately not using:
#   • A separate load balancer (no need for one replica)
#   • Cloud NAT, MIGs, Cloud Run, GKE — all overkill
#   • Secret Manager — secrets ride the VM metadata; they're already
#     scoped to one customer's project anyway. Swappable later.

locals {
  prefix = var.name

  # Rendered into a single docker-compose-ish unit by cloud-init. The
  # values are interpolated and end up in /etc/voitta/env.list which is
  # passed into the container as --env-file.
  voitta_env = merge(
    {
      VOITTA_DATA_DIR                  = "/data"
      VOITTA_ROOT_PATH                 = "/data/folders"
      VOITTA_USERS_FILE                = "/etc/voitta/users.txt"
      VOITTA_PORT                      = "8000"
      VOITTA_SUPER_ADMINS              = join(",", var.super_admins)
      VOITTA_GOOGLE_AUTH_CLIENT_ID     = var.google_oauth_client_id
      VOITTA_GOOGLE_AUTH_CLIENT_SECRET = var.google_oauth_client_secret
      VOITTA_SESSION_SECRET            = var.session_secret
    },
    var.extra_env,
  )

  users_txt = join("\n", concat(
    ["# Managed by Terraform — extra_users variable. Do not edit by hand."],
    var.extra_users,
    [""],
  ))
}

# ---------------------------------------------------------------------------
# APIs
# ---------------------------------------------------------------------------
resource "google_project_service" "apis" {
  for_each = toset([
    "compute.googleapis.com",
    "iam.googleapis.com",
  ])

  project            = var.project_id
  service            = each.key
  disable_on_destroy = false
}

# ---------------------------------------------------------------------------
# Static external IP — output to the operator for DNS wiring.
# ---------------------------------------------------------------------------
resource "google_compute_address" "this" {
  name    = "${local.prefix}-ip"
  project = var.project_id
  region  = var.region

  depends_on = [google_project_service.apis]
}

# ---------------------------------------------------------------------------
# Data PD — survives VM recreation. Holds SQLite, CAS, embedded Qdrant,
# uploads, Drive mirror.
# ---------------------------------------------------------------------------
resource "google_compute_disk" "data" {
  name    = "${local.prefix}-data"
  project = var.project_id
  zone    = var.zone
  type    = var.data_disk_type
  size    = var.data_disk_gb

  # Don't let ``terraform destroy`` remove the data disk by accident; the
  # operator must explicitly drop it. Detach + delete the disk separately
  # if a real teardown is intended.
  lifecycle {
    prevent_destroy = false # keep false in test envs; flip to true for prod
  }

  depends_on = [google_project_service.apis]
}

# ---------------------------------------------------------------------------
# Firewall — open 80/443 if requested. SSH stays closed; use IAP tunnels.
# ---------------------------------------------------------------------------
resource "google_compute_firewall" "http" {
  count   = var.open_http ? 1 : 0
  name    = "${local.prefix}-allow-http"
  network = "default"
  project = var.project_id

  allow {
    protocol = "tcp"
    ports    = ["80", "443"]
  }

  source_ranges = ["0.0.0.0/0"]
  target_tags   = ["${local.prefix}-vm"]

  depends_on = [google_project_service.apis]
}

# ---------------------------------------------------------------------------
# The VM itself.
# ---------------------------------------------------------------------------
resource "google_compute_instance" "this" {
  name         = "${local.prefix}-vm"
  project      = var.project_id
  zone         = var.zone
  machine_type = var.machine_type

  tags = ["${local.prefix}-vm"]

  boot_disk {
    initialize_params {
      # Container-Optimized OS — minimal, container-first, auto-updating.
      # No package manager (intentional) — everything runs as containers.
      image = "projects/cos-cloud/global/images/family/cos-stable"
      size  = 30
      # C-family machines (c4, c4d, c4a) require Hyperdisk-Balanced for
      # boot. ``pd-balanced`` is rejected with a 400. ``hyperdisk-balanced``
      # is the C4 default and what GCE itself uses if you spin one up
      # via the console.
      type = "hyperdisk-balanced"
    }
  }

  attached_disk {
    source      = google_compute_disk.data.self_link
    device_name = "voitta-data"
    mode        = "READ_WRITE"
  }

  network_interface {
    network = "default"

    access_config {
      nat_ip = google_compute_address.this.address
    }
  }

  metadata = {
    # Drop the SSH key only when one was supplied; otherwise leave the
    # metadata key absent so the VM doesn't accept project-wide keys.
    ssh-keys = var.ssh_pubkey == "" ? null : "voitta:${var.ssh_pubkey}"

    # cloud-init / cloud-config for COS. ``user-data`` is the standard
    # key COS reads on first boot.
    user-data = templatefile("${path.module}/cloud-init.yaml.tftpl", {
      image_uri = var.image_uri
      env_pairs = local.voitta_env
      users_txt = local.users_txt
      domain    = var.domain
    })
  }

  service_account {
    # The default Compute Engine SA is fine for v1 — pulling images from
    # GHCR doesn't need GCP IAM, and we don't read other GCP services
    # from the VM.
    scopes = ["cloud-platform"]
  }

  # Allow ``terraform apply`` to recreate the VM (e.g. when image_uri
  # changes) without it being blocked by an existing instance.
  allow_stopping_for_update = true
}
