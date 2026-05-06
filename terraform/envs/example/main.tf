# Example consumer of the voitta-rag module.
#
# Copy this directory to ``terraform/envs/<customer>/``, fill in
# ``terraform.tfvars`` (see terraform.tfvars.example), and ``terraform
# apply``. Each customer gets its own state file via backend.tf.

terraform {
  required_version = ">= 1.6"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.40, < 7.0"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = ">= 2.30"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

# kubernetes provider is configured against the cluster the module
# creates. Two-step apply: first the cluster comes up, then this provider
# block can authenticate. If you hit a timing error on the very first
# apply, run ``terraform apply -target=module.voitta_rag.google_container_node_pool.primary``
# once, then a second ``terraform apply``.
data "google_client_config" "default" {}

data "google_container_cluster" "voitta" {
  name     = module.voitta_rag.cluster_name
  location = var.zone
  project  = var.project_id

  depends_on = [module.voitta_rag]
}

provider "kubernetes" {
  host                   = "https://${data.google_container_cluster.voitta.endpoint}"
  token                  = data.google_client_config.default.access_token
  cluster_ca_certificate = base64decode(data.google_container_cluster.voitta.master_auth[0].cluster_ca_certificate)
}

module "voitta_rag" {
  source = "../../modules/voitta-rag"

  project_id = var.project_id
  region     = var.region
  zone       = var.zone

  name      = var.name
  image_uri = var.image_uri

  allowed_domains = var.allowed_domains
  extra_users     = var.extra_users

  google_oauth_client_id     = var.google_oauth_client_id
  google_oauth_client_secret = var.google_oauth_client_secret
}

# Pass-through outputs so ``terraform output`` from the env shows the
# things the operator needs after apply.
output "ingress_ip" {
  value = module.voitta_rag.ingress_ip
}

output "redirect_uri" {
  value = module.voitta_rag.redirect_uri
}

output "cluster_name" {
  value = module.voitta_rag.cluster_name
}

output "namespace" {
  value = module.voitta_rag.namespace
}
