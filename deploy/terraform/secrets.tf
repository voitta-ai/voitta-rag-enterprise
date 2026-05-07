# Secret Manager *resources* — terraform owns the resource shape and
# IAM bindings; secret values are populated out-of-band via
# deploy/scripts/populate_secrets.sh from a local .env.secrets file.
#
# Splitting it this way keeps secret values out of terraform state and
# off the operator's terraform plan output. Customer rotates a secret
# by editing .env.secrets and re-running the script — every run adds
# a new version, the latest is what the app reads at boot via
# deploy/docker-entrypoint.sh's `sm://` resolver (#9).

locals {
  # Names + descriptions for every env-level secret the app reads.
  # Values like gh_pat / gd_refresh_token / gd_service_account_json
  # are NOT here — those are per-folder, stored encrypted in
  # folder_sync_sources via #10's services/crypto.py.
  app_secrets = {
    "voitta-google-auth-client-id" = {
      description = "OAuth client ID for Sign in with Google."
    }
    "voitta-google-auth-client-secret" = {
      description = "OAuth client secret for Sign in with Google."
    }
    "voitta-session-secret" = {
      description = "HMAC key for the SPA's session cookie. Rotate to force-logout all users."
    }
    "voitta-dense-version" = {
      description = "Override VOITTA_DENSE_VERSION. Bumping triggers a re-embed migration."
    }
    "voitta-sparse-version" = {
      description = "Override VOITTA_SPARSE_VERSION."
    }
    "voitta-image-version" = {
      description = "Override VOITTA_IMAGE_VERSION."
    }
  }
}

resource "google_secret_manager_secret" "app" {
  for_each = local.app_secrets

  secret_id = each.key
  labels    = var.labels

  replication {
    auto {
      # CMEK on the auto-replicated secret when enable_cmek=true.
      # Reuses the keyring + key from #7 — same Cloud KMS key
      # encrypts disk and secret payloads.
      dynamic "customer_managed_encryption" {
        for_each = var.enable_cmek ? [1] : []
        content {
          kms_key_name = local.kms_crypto_key_id
        }
      }
    }
  }

  # Don't auto-destroy on terraform destroy. If a customer pulls the
  # stack apart and rebuilds, secret values typed into a console once
  # should not vanish along with the resource.
  lifecycle {
    prevent_destroy = false # flip to true once a real customer is on it
  }

  # The service-agent IAM binding (#7) on the KMS key must be in
  # place before this secret can be created when CMEK is on.
  depends_on = [
    google_kms_crypto_key_iam_member.secretmanager_agent,
  ]
}

# VM service account → secretAccessor on each secret. Granted at the
# secret level (least privilege) rather than project level.
resource "google_secret_manager_secret_iam_member" "vm_accessor" {
  for_each = local.app_secrets

  secret_id = google_secret_manager_secret.app[each.key].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.vm.email}"
}
