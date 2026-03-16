output "service_url" {
  description = "Public URL of the PitchMirror Cloud Run service"
  value       = google_cloud_run_v2_service.pitchmirror.uri
}

output "service_account_email" {
  description = "Service account email used by Cloud Run"
  value       = google_service_account.pitchmirror_sa.email
}

output "firestore_user_history_index" {
  description = "Composite Firestore index used by user-scoped session history queries"
  value       = google_firestore_index.sessions_by_user_created_at.name
}
