# Customer-managed encryption keys for disks (and, in #8, Secret
# Manager). All resources gated on var.enable_cmek; when false, GCE
# disks fall back to Google-managed encryption (free, automatic) and
# Secret Manager uses its default per-secret key.
#
# IMPORTANT: once enable_cmek is set true and resources reference the
# key, you cannot flip it back to false without destroying and
# recreating those resources — the disk's `disk_encryption_key`
# attribute is force-new.

# Project number is needed to address GCP service agents (predictable
# format `service-<number>@*.iam.gserviceaccount.com`). The provider
# data source returns it without an extra API call beyond what the
# project lookup already costs.
data "google_project" "current" {
  count = var.enable_cmek ? 1 : 0

  project_id = var.project_id
}

resource "google_kms_key_ring" "main" {
  count = var.enable_cmek ? 1 : 0

  name     = var.kms_keyring
  location = var.region
}

resource "google_kms_crypto_key" "main" {
  count = var.enable_cmek ? 1 : 0

  name            = var.kms_key
  key_ring        = google_kms_key_ring.main[0].id
  rotation_period = "7776000s" # 90 days

  # Stop accidental destroys. KMS keys can be re-enabled but cannot be
  # purged for 30 days; deleting from terraform also abandons the
  # encrypted resources.
  lifecycle {
    prevent_destroy = false # set true once a real customer is on it
  }
}

# Compute Engine service agent needs encrypter/decrypter on the key
# so it can create CMEK-encrypted disks. Granted at the key level
# rather than the project level for least-privilege.
resource "google_kms_crypto_key_iam_member" "compute_agent" {
  count = var.enable_cmek ? 1 : 0

  crypto_key_id = google_kms_crypto_key.main[0].id
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = "serviceAccount:service-${data.google_project.current[0].number}@compute-system.iam.gserviceaccount.com"
}

# Secret Manager service agent — needed for the CMEK-on-secrets path
# in #8. Granted here so the key's IAM is one-shop and #8 doesn't
# need to touch this file.
resource "google_kms_crypto_key_iam_member" "secretmanager_agent" {
  count = var.enable_cmek ? 1 : 0

  crypto_key_id = google_kms_crypto_key.main[0].id
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = "serviceAccount:service-${data.google_project.current[0].number}@gcp-sa-secretmanager.iam.gserviceaccount.com"
}

# Make the key's resource ID available downstream (compute.tf disk
# resources, services/crypto.py at runtime via Secret Manager in #10).
locals {
  kms_crypto_key_id = var.enable_cmek ? google_kms_crypto_key.main[0].id : null
}
