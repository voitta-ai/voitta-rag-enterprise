# Service account the VM runs as. Roles are scoped to what later
# issues actually use:
#   - secretmanager.secretAccessor: read app secrets at boot (#9).
#   - cloudkms.cryptoKeyEncrypterDecrypter: envelope-encrypt
#     folder_sync_sources rows (#10) and decrypt CMEK disk (#7).
#   - logging.logWriter / monitoring.metricWriter: ship logs and
#     metrics to GCP without ops-agent extra setup.
#   - artifactregistry.reader: optional. GHCR is public for now, so the
#     VM doesn't need it; granting it anyway means a customer who
#     mirrors the image into their own AR works without an IAM PR.
resource "google_service_account" "vm" {
  account_id   = "${local.prefix}-vm"
  display_name = "Service account for the ${local.prefix} VM"
}

locals {
  vm_sa_roles = [
    "roles/secretmanager.secretAccessor",
    "roles/cloudkms.cryptoKeyEncrypterDecrypter",
    "roles/logging.logWriter",
    "roles/monitoring.metricWriter",
    "roles/artifactregistry.reader",
  ]
}

resource "google_project_iam_member" "vm_sa" {
  for_each = toset(local.vm_sa_roles)

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.vm.email}"
}

# Data disk. SQLite, CAS, Qdrant, and the model cache live here. Mount
# point and filesystem creation happen in cloud-init in #4. We attach
# it deviceName=voitta-data so the cloud-init script can find it
# deterministically as /dev/disk/by-id/google-voitta-data.
resource "google_compute_disk" "data" {
  name = "${local.prefix}-data"
  type = var.disk_type
  zone = var.zone
  size = var.disk_size_gb

  labels = var.labels

  # CMEK when enabled. The kms_key_self_link sub-attribute is
  # force-new on the disk; once set, you can't switch back to
  # Google-managed without recreating (and losing) the volume.
  dynamic "disk_encryption_key" {
    for_each = var.enable_cmek ? [1] : []
    content {
      kms_key_self_link = local.kms_crypto_key_id
    }
  }
}

# cloud-init payload. Files referenced via file() are repo-tracked
# source of truth in deploy/{systemd,Caddyfile.tpl}; we indent them to
# slot under the YAML write_files block scalar. Each substitution
# point in the template carries a manual 6-space prefix because
# terraform's indent() leaves the first line alone.
locals {
  enable_caddy = !var.create_load_balancer

  # Render the Caddyfile by substituting ${DOMAIN} via templatefile.
  # Empty string when Caddy is disabled; the template branch won't
  # reference it anyway, but locals must always evaluate.
  caddyfile_rendered = local.enable_caddy ? templatefile(
    "${path.module}/../Caddyfile.tpl",
    { DOMAIN = var.domain },
  ) : ""

  cloud_init_user_data = templatefile(
    "${path.module}/../cloud-init.yaml.tftpl",
    {
      image_repo              = var.image_repo
      image_tag               = var.image_tag
      voitta_service_indented = indent(6, file("${path.module}/../systemd/voitta.service"))
      enable_caddy            = local.enable_caddy
      caddyfile_indented      = indent(6, local.caddyfile_rendered)
      caddy_service_indented  = indent(6, file("${path.module}/../systemd/caddy.service"))
      # Empty string when enable_cmek=false; the template branch
      # keys off non-empty to render the VOITTA_KMS_KEY line.
      kms_key_resource = local.kms_crypto_key_id == null ? "" : local.kms_crypto_key_id
    }
  )
}

resource "google_compute_instance" "vm" {
  name         = "${local.prefix}-vm"
  machine_type = var.machine_type
  zone         = var.zone
  # `-lb` tag is harmless when create_load_balancer = false (no
  # firewall rule references it). Adding it unconditionally keeps the
  # VM resource shape stable across the toggle.
  tags = ["${local.prefix}-web", "${local.prefix}-iap", "${local.prefix}-lb"]

  boot_disk {
    initialize_params {
      image = var.boot_disk_image
      size  = var.boot_disk_size_gb
    }

    kms_key_self_link = local.kms_crypto_key_id
  }

  attached_disk {
    source      = google_compute_disk.data.id
    device_name = "voitta-data"
    mode        = "READ_WRITE"
  }

  network_interface {
    subnetwork = google_compute_subnetwork.subnet.id

    access_config {
      nat_ip       = google_compute_address.vm_ip.address
      network_tier = "PREMIUM"
    }
  }

  service_account {
    email  = google_service_account.vm.email
    scopes = ["cloud-platform"]
  }

  shielded_instance_config {
    enable_secure_boot          = true
    enable_vtpm                 = true
    enable_integrity_monitoring = true
  }

  labels = var.labels

  metadata = {
    # COS reads cloud-init payloads from `user-data`.
    # https://cloud.google.com/container-optimized-os/docs/how-to/create-configure-instance#cloud-init
    user-data = local.cloud_init_user_data

    # No project-wide SSH keys; rely on IAP + OS Login.
    block-project-ssh-keys = "TRUE"
    enable-oslogin         = "TRUE"
  }

  # cloud-init runs once at first boot. Subsequent image upgrades go
  # through `make deploy-upgrade` (#13), which SSHs in and updates
  # /etc/voitta/image.env. Freezing metadata here means a later
  # `terraform apply` won't fight that out-of-band change.
  lifecycle {
    ignore_changes = [
      metadata,
      metadata_startup_script,
    ]
  }
}
