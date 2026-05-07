output "vm_external_ip" {
  description = "Static external IP of the VM. Point your A record here when create_load_balancer=false; otherwise use lb_external_ip."
  value       = google_compute_address.vm_ip.address
}

output "lb_external_ip" {
  description = "Global LB IP. Null when create_load_balancer=false. Customer's DNS A record for var.domain points here."
  value       = try(google_compute_global_address.lb[0].address, null)
}

output "vm_name" {
  description = "Compute Engine instance name. Used by the deploy/scripts/* helpers."
  value       = google_compute_instance.vm.name
}

output "vm_zone" {
  description = "Zone the VM lives in."
  value       = google_compute_instance.vm.zone
}

output "vm_service_account_email" {
  description = "Service account the VM runs as. Grant additional IAM bindings against this principal."
  value       = google_service_account.vm.email
}

output "ssh_command" {
  description = "Copy/paste command to SSH into the VM via IAP."
  value       = "gcloud compute ssh ${google_compute_instance.vm.name} --zone=${google_compute_instance.vm.zone} --project=${var.project_id} --tunnel-through-iap"
}

output "secret_ids" {
  description = "Secret Manager secret IDs declared by terraform. deploy/scripts/populate_secrets.sh iterates this list."
  value       = sort([for s in google_secret_manager_secret.app : s.secret_id])
}
