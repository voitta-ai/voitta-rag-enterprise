# First-customer test deploy in project ``voitta-report-builder``.
#
# OAuth is enabled. Credentials live in terraform.tfvars (gitignored) so
# the client secret never lands in source. The variable defaults below
# give an empty fallback so a fresh checkout still ``init``s.

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

variable "google_oauth_client_id" {
  type      = string
  sensitive = true
  default   = ""
}

variable "google_oauth_client_secret" {
  type      = string
  sensitive = true
  default   = ""
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

  # Bootstrap super-admins for THIS deployment. Hardcoded into the env so
  # a fresh Docker.raw / wiped data PD still leaves at least one human
  # able to sign in and rebuild the allowlists. Customer envs override.
  super_admins = ["roman.semein@gmail.com"]

  google_oauth_client_id     = var.google_oauth_client_id
  google_oauth_client_secret = var.google_oauth_client_secret
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
