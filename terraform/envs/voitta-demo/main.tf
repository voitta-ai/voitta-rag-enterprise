# First-customer test deploy in project ``voitta-report-builder``.
#
# Single VM. No OAuth. Auto-signed-in as VOITTA_DEV_USER for the test;
# real customer deploys flip OAuth on by populating client_id/secret.

terraform {
  required_version = ">= 1.6"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.40, < 7.0"
    }
  }

  backend "gcs" {
    bucket = "voitta-tfstate-report-builder"
    prefix = "voitta-demo"
  }
}

provider "google" {
  project = "voitta-report-builder"
  region  = "us-central1"
}

module "voitta_rag" {
  source = "../../modules/voitta-rag"

  project_id   = "voitta-report-builder"
  region       = "us-central1"
  zone         = "us-central1-a"
  name         = "voitta-rag"
  machine_type = "c4-standard-8"

  image_uri = "ghcr.io/voitta-ai/voitta-rag-enterprise:sha-7d03e12"

  domain       = "rag-enterprise-demo.voitta.ai"
  data_disk_gb = 200

  # Test deploy — OAuth disabled, auto-sign-in as the dev user.
  allowed_domains            = ["voitta.ai"]
  google_oauth_client_id     = ""
  google_oauth_client_secret = ""

  extra_env = {
    VOITTA_DEV_USER = "demo@voitta.ai"
  }
}

output "external_ip" {
  value = module.voitta_rag.external_ip
}

output "vm_name" {
  value = module.voitta_rag.vm_name
}

output "vm_zone" {
  value = module.voitta_rag.vm_zone
}
