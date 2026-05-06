variable "project_id" {
  type        = string
  description = "Customer GCP project the deployment lives in."
}

variable "region" {
  type        = string
  description = "GCP region (e.g. \"us-central1\"). Used for the static IP and Artifact Registry pulls."
}

variable "zone" {
  type        = string
  description = "GCP zone for the GKE cluster + node pool (e.g. \"us-central1-a\"). Single-zone — there is no regional spread; the single replica only runs here."
}

variable "name" {
  type        = string
  description = "Short identifier for this deployment (lowercase, dashes). Used as a prefix for cluster, PVC, ingress, IP. Pick something stable per customer."
  default     = "voitta-rag"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,28}[a-z0-9]$", var.name))
    error_message = "name must be lowercase, start with a letter, contain only letters/digits/dashes, and be 3-30 chars."
  }
}

variable "image_uri" {
  type        = string
  description = "Full container image reference, including tag (e.g. \"ghcr.io/voitta-ai/voitta-rag-enterprise:v0.1.0\"). The image is pulled by the node, so a public registry is simplest; for a private registry, also configure imagePullSecrets out-of-band."
}

variable "machine_type" {
  type        = string
  description = "GKE node machine type. Default c4-standard-8 has AVX-512 + AMX which materially accelerates ONNX/oneDNN-backed embedders. Fall back to c3-standard-8 if c4 is unavailable in the region."
  default     = "c4-standard-8"
}

variable "data_disk_gb" {
  type        = number
  description = "Size of the persistent disk holding SQLite, CAS, embedded Qdrant, uploads, and Drive mirrors. 200GB is plenty for ~100k pages; bump for larger corpora."
  default     = 200
}

variable "data_disk_type" {
  type        = string
  description = "PD class. balanced is the right default; ssd helps Qdrant query latency under heavy load."
  default     = "pd-balanced"
}

variable "allowed_domains" {
  type        = list(string)
  description = "Email domains permitted to sign in. Combined with extra_users — match either to admit. Empty list + empty extra_users denies every sign-in (deliberate fail-loud default)."
  default     = []
}

variable "extra_users" {
  type        = list(string)
  description = "Individual email addresses outside allowed_domains that can also sign in (consultants, contractors). Rendered into users.txt and mounted into the pod."
  default     = []
}

variable "google_oauth_client_id" {
  type        = string
  description = "OAuth 2.0 Client ID (type: Web application). The redirect URI to register on the Google side is output as `redirect_uri` after apply."
  sensitive   = true
}

variable "google_oauth_client_secret" {
  type        = string
  description = "OAuth 2.0 Client secret matching google_oauth_client_id."
  sensitive   = true
}

variable "session_secret" {
  type        = string
  description = "Cookie-signing secret. Leave empty and the app generates + persists one under the data PD on first boot."
  sensitive   = true
  default     = ""
}

variable "extra_env" {
  type        = map(string)
  description = "Free-form VOITTA_* env vars to inject. Use for tuning (e.g. VOITTA_PDF_PARSE_TIMEOUT_S, VOITTA_NEARBY_RADIUS) without editing the module."
  default     = {}
  sensitive   = true
}
