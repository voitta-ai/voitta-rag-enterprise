variable "project_id" {
  type = string
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "zone" {
  type    = string
  default = "us-central1-a"
}

variable "name" {
  type    = string
  default = "voitta-rag"
}

variable "image_uri" {
  type = string
}

variable "allowed_domains" {
  type    = list(string)
  default = []
}

variable "extra_users" {
  type    = list(string)
  default = []
}

variable "google_oauth_client_id" {
  type      = string
  sensitive = true
}

variable "google_oauth_client_secret" {
  type      = string
  sensitive = true
}
