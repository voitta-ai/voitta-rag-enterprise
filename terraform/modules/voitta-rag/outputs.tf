output "external_ip" {
  description = "Static external IPv4 attached to the VM. Point your DNS A record at this."
  value       = google_compute_address.this.address
}

output "vm_name" {
  description = "Compute Engine instance name. Use with `gcloud compute ssh`."
  value       = google_compute_instance.this.name
}

output "vm_zone" {
  description = "Zone the VM lives in."
  value       = google_compute_instance.this.zone
}

output "redirect_uri_template" {
  description = "Authorized redirect URI to register on the OAuth client. Replace <host> with the FQDN you wire DNS for."
  value       = "https://<host>/api/auth/google/callback"
}
