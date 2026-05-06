# All inputs come in via TF_VAR_* environment variables. The customer
# fills in `.env.terraform` and the `make deploy-*` targets source it
# before invoking terraform. No `-var-file` indirection.

variable "project_id" {
  description = "GCP project ID where everything in this stack lives."
  type        = string
}

variable "region" {
  description = "GCP region."
  type        = string
  default     = "us-central1"
}

variable "zone" {
  description = "GCP zone within the region. The VM is single-zone."
  type        = string
  default     = "us-central1-a"
}

variable "name_prefix" {
  description = <<-EOT
    Prefix applied to all resource names so a customer can run multiple
    voitta-rag stacks side-by-side in one project (rare, but cheap to
    support).
  EOT
  type        = string
  default     = "voitta-rag"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,28}[a-z0-9]$", var.name_prefix))
    error_message = "name_prefix must match GCE resource name rules: lowercase, digits, hyphens; start with a letter; <= 30 chars."
  }
}

# ----- Image -----

variable "image_repo" {
  description = <<-EOT
    Container image repository the VM pulls from. Defaults to the
    public GHCR image published by the release workflow (#2). Customers
    who mirror into their own Artifact Registry override this.
  EOT
  type        = string
  default     = "ghcr.io/voitta-ai/voitta-rag-enterprise"
}

variable "image_tag" {
  description = <<-EOT
    Image tag to pull on first boot. Subsequent upgrades go through
    `make deploy-upgrade` (issue #13), not by changing this and running
    `terraform apply` — the VM's metadata is in `lifecycle.ignore_changes`
    so terraform doesn't try to reset it.
  EOT
  type        = string
  default     = "latest"
}

# ----- Compute -----

variable "machine_type" {
  description = <<-EOT
    Compute Engine machine type. Tier A default is e2-standard-4 (4
    vCPU / 16 GB) which is enough for ~10 users on CPU-only embedders.
    Bump to g2-standard-4 (with an L4 GPU) for Tier B; that move is
    issue #13 and not gated here.
  EOT
  type        = string
  default     = "e2-standard-4"
}

variable "disk_size_gb" {
  description = "Persistent disk size for app data (CAS + SQLite + Qdrant + model cache)."
  type        = number
  default     = 200
}

variable "disk_type" {
  description = "Persistent disk type. pd-balanced is the right default; pd-ssd costs more for marginal RAG-workload gains."
  type        = string
  default     = "pd-balanced"

  validation {
    condition     = contains(["pd-standard", "pd-balanced", "pd-ssd", "hyperdisk-balanced"], var.disk_type)
    error_message = "disk_type must be one of pd-standard, pd-balanced, pd-ssd, hyperdisk-balanced."
  }
}

variable "boot_disk_image" {
  description = "Boot disk image. Container-Optimized OS by default; Ubuntu LTS works too if you prefer."
  type        = string
  default     = "projects/cos-cloud/global/images/family/cos-stable"
}

variable "boot_disk_size_gb" {
  description = "Boot disk size. 30 GB is plenty for COS plus a pulled image; the data disk is separate."
  type        = number
  default     = 30
}

# ----- Network -----

variable "subnet_cidr" {
  description = "CIDR for the single private subnet the VM lives on."
  type        = string
  default     = "10.20.0.0/24"
}

variable "ingress_cidrs_http" {
  description = "Source CIDRs allowed to hit ports 80/443. Default is open internet; tighten for internal-only deployments."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

# ----- Labels -----

variable "labels" {
  description = "Labels applied to every resource that supports them. Useful for cost-allocation reports."
  type        = map(string)
  default = {
    app       = "voitta-rag"
    component = "tier-a"
    managed   = "terraform"
  }
}
