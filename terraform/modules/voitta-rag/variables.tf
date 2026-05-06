variable "project_id" {
  type        = string
  description = "Customer GCP project the deployment lives in."
}

variable "region" {
  type        = string
  description = "GCP region (e.g. \"us-central1\"). Used for the static IP."
}

variable "zone" {
  type        = string
  description = "GCP zone for the VM and its data PD. Single-zone — no regional spread."
}

variable "name" {
  type        = string
  description = "Short identifier for this deployment (lowercase, dashes). Prefix for VM, PD, and IP."
  default     = "voitta-rag"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,28}[a-z0-9]$", var.name))
    error_message = "name must be lowercase, start with a letter, contain only letters/digits/dashes, and be 3-30 chars."
  }
}

variable "machine_type" {
  type        = string
  description = "Compute Engine machine type. Default c4-standard-8 has AVX-512 + AMX which materially accelerates ONNX/oneDNN-backed embedders."
  default     = "c4-standard-8"
}

variable "image_uri" {
  type        = string
  description = "Full container image reference, including tag (e.g. \"ghcr.io/voitta-ai/voitta-rag-enterprise:v0.1.0\")."
}

variable "data_disk_gb" {
  type        = number
  description = "Size of the persistent disk holding SQLite, CAS, embedded Qdrant, uploads, and Drive mirrors."
  default     = 200
}

variable "data_disk_type" {
  type        = string
  description = "PD class. ``hyperdisk-balanced`` is required when machine_type is C/C4-family; ``pd-balanced`` works for older N/E2 series."
  default     = "hyperdisk-balanced"
}

variable "super_admins" {
  type        = list(string)
  description = "Bootstrap admin emails. Always admitted at sign-in (block-list aside) and stamped with is_admin=true on every login. Used to make sure at least one human can recover from an empty/lockout-out allowlist. In a real deploy, set to one email you control; the demo env hardcodes the operator."
  default     = []
}

variable "allowed_domains" {
  type        = list(string)
  description = "Legacy. No longer consulted by the sign-in gate — admins manage the live allowlist via the UI (persisted on the data PD). Kept as a variable so older tfvars files don't break."
  default     = []
}

variable "extra_users" {
  type        = list(string)
  description = "Legacy. Same status as allowed_domains — admin UI is the live source of truth now."
  default     = []
}

variable "google_oauth_client_id" {
  type        = string
  description = "OAuth 2.0 Client ID. Empty string disables Google sign-in."
  sensitive   = true
  default     = ""
}

variable "google_oauth_client_secret" {
  type        = string
  description = "OAuth 2.0 Client secret."
  sensitive   = true
  default     = ""
}

variable "session_secret" {
  type        = string
  description = "Cookie-signing secret. Empty string makes the app generate + persist one on the data PD on first boot."
  sensitive   = true
  default     = ""
}

variable "extra_env" {
  type        = map(string)
  description = "Free-form VOITTA_* env vars to inject — used for tuning or to set VOITTA_DEV_USER in test deploys."
  default     = {}
  sensitive   = true
}

variable "ssh_pubkey" {
  type        = string
  description = "Optional SSH public key to drop on the VM (user 'voitta'). Empty = no SSH access; use IAP tunnels via gcloud instead."
  default     = ""
}

variable "open_http" {
  type        = bool
  description = "When true, opens 80/443 to 0.0.0.0/0 so a browser can reach Caddy. Set false when fronting with an external LB or restricting access."
  default     = true
}

variable "domain" {
  type        = string
  description = "Public hostname this deploy answers on (e.g. \"rag.customer.com\"). When set, Caddy issues a Let's Encrypt cert via HTTP-01 on port 80 and serves the app on HTTPS, with all HTTP traffic 308-redirected to HTTPS. When empty, Caddy serves plain HTTP on port 80 — for early bring-up before DNS is wired."
  default     = ""
}
