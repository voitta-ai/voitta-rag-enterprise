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
}

# The VM itself. No metadata.user-data (cloud-init) yet — that lands in
# #4. Right now this just gets you a bootable host with the right SA,
# tags, IP, and disk attached.
resource "google_compute_instance" "vm" {
  name         = "${local.prefix}-vm"
  machine_type = var.machine_type
  zone         = var.zone
  tags         = ["${local.prefix}-web", "${local.prefix}-iap"]

  boot_disk {
    initialize_params {
      image = var.boot_disk_image
      size  = var.boot_disk_size_gb
    }
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

  # The cloud-init script (#4) will mutate the disk on first boot; we
  # don't want a tag/label tweak to trigger a VM rebuild that wipes the
  # boot-time work. Lock the boot image to changes only.
  lifecycle {
    ignore_changes = [
      metadata,
      metadata_startup_script,
    ]
  }
}
