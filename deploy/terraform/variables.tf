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

# ----- TLS / domain -----

variable "domain" {
  description = <<-EOT
    Hostname the app serves under. Used by Caddy (Let's Encrypt) and
    later by the optional Cloud LB managed cert (#6). Customer must
    point an A record at the VM's static IP BEFORE first apply, else
    ACME HTTP-01 fails and Caddy temporarily serves a self-signed
    cert. Leave empty to bring Caddy up without TLS (HTTP only) for
    smoke testing.
  EOT
  type        = string
  default     = ""

  validation {
    condition     = var.domain == "" || can(regex("^[a-zA-Z0-9.-]+$", var.domain))
    error_message = "domain must be a valid hostname (letters, digits, dots, hyphens) or the empty string."
  }
}

variable "create_load_balancer" {
  description = <<-EOT
    Toggle for the Tier A TLS path:
      false (default): Caddy on the VM auto-issues a Let's Encrypt
        cert. Cheap, no LB cost.
      true: Cloud Load Balancer + Google-managed SSL cert (issue #6).
        Caddy is skipped on the VM; the app is reached only via the LB.
  EOT
  type        = bool
  default     = false
}

variable "domain_aliases" {
  description = <<-EOT
    Additional hostnames to include on the managed SSL cert when
    create_load_balancer = true (e.g. www. aliases or vanity domains).
    Each must be DNS-resolvable to the LB IP before terraform apply,
    or cert provisioning stalls. Empty by default.
  EOT
  type        = list(string)
  default     = []
}

# ----- Encryption -----

variable "enable_cmek" {
  description = <<-EOT
    When true, disks are encrypted with a customer-managed key (CMEK)
    in Cloud KMS instead of the default Google-managed key. Same key
    is used for Secret Manager secrets in #8 and for app-level
    envelope encryption in #10. Once turned on, never turn off — the
    disk would have to be recreated.
  EOT
  type        = bool
  default     = false
}

variable "kms_keyring" {
  description = "Cloud KMS keyring name. Created when enable_cmek=true."
  type        = string
  default     = "voitta"
}

variable "kms_key" {
  description = "Cloud KMS crypto-key name. Created under the keyring with 90-day rotation."
  type        = string
  default     = "disk-and-secrets"
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
