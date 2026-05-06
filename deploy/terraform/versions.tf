# Provider and Terraform version pinning. Bump together; the google
# provider's resource shape sometimes changes in minor versions.

terraform {
  required_version = ">= 1.6"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }

  # State backend.
  #
  # Default: local state file under deploy/terraform/terraform.tfstate.
  # Fine for first-time bring-up, single-operator installs, and
  # destroy-and-rebuild iteration. NOT fine for production: state holds
  # service-account emails and other identifiers, and a lost laptop
  # losing the state file means terraform cannot destroy what it created.
  #
  # For prod: copy the snippet below into a new versions_override.tf,
  # create the bucket once with `gsutil mb`, and re-run `terraform init`.
  #
  #   backend "gcs" {
  #     bucket = "voitta-tfstate-<project-id>"
  #     prefix = "voitta-rag/tier-a"
  #   }
}
