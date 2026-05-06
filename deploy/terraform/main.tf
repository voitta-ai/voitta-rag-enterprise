# Provider config. We do NOT plumb credentials through terraform vars;
# the customer authenticates locally via
#
#   gcloud auth application-default login
#
# (or sets GOOGLE_APPLICATION_CREDENTIALS for an SA key, if they must).
# Keeping creds out of state and out of `.env.terraform` reduces blast
# radius if either leaks.

provider "google" {
  project = var.project_id
  region  = var.region
  zone    = var.zone
}

# Convenience locals. Every resource name derives from this so a
# rename only happens in one place.
locals {
  prefix = var.name_prefix

  # GCP's IAP TCP-forwarding gateway always announces from this CIDR.
  # Used by the firewall rule that allows SSH only via IAP.
  iap_tcp_forwarders_cidr = "35.235.240.0/20"
}
