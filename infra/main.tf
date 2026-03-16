terraform {
  required_version = ">= 1.5"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

# ── Enable required APIs ──────────────────────────────────────────
resource "google_project_service" "run" {
  service            = "run.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "firestore" {
  service            = "firestore.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "secretmanager" {
  service            = "secretmanager.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "artifactregistry" {
  service            = "artifactregistry.googleapis.com"
  disable_on_destroy = false
}

# ── Firestore database ────────────────────────────────────────────
resource "google_firestore_database" "pitchmirror" {
  name        = "(default)"
  location_id = var.region
  type        = "FIRESTORE_NATIVE"

  depends_on = [google_project_service.firestore]
}

# Composite index for user-scoped session history queries:
# where("user_id", "==", user_id).order_by("created_at", DESC)
resource "google_firestore_index" "sessions_by_user_created_at" {
  project    = var.project_id
  database   = google_firestore_database.pitchmirror.name
  collection = var.firestore_collection

  fields {
    field_path = "user_id"
    order      = "ASCENDING"
  }

  fields {
    field_path = "created_at"
    order      = "DESCENDING"
  }

  depends_on = [google_firestore_database.pitchmirror]
}

# ── Secret Manager: Gemini API key ────────────────────────────────
resource "google_secret_manager_secret" "gemini_api_key" {
  secret_id = var.gemini_secret_id

  replication {
    auto {}
  }

  depends_on = [google_project_service.secretmanager]
}

# ── Service account for Cloud Run ─────────────────────────────────
resource "google_service_account" "pitchmirror_sa" {
  account_id   = "pitchmirror-sa"
  display_name = "PitchMirror Cloud Run Service Account"
}

# Allow the SA to read secrets
resource "google_secret_manager_secret_iam_member" "sa_secret_access" {
  secret_id = google_secret_manager_secret.gemini_api_key.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.pitchmirror_sa.email}"
}

# Allow the SA to read/write Firestore
resource "google_project_iam_member" "sa_firestore" {
  project = var.project_id
  role    = "roles/datastore.user"
  member  = "serviceAccount:${google_service_account.pitchmirror_sa.email}"
}

# ── Cloud Run service ─────────────────────────────────────────────
resource "google_cloud_run_v2_service" "pitchmirror" {
  name     = var.service_name
  location = var.region

  template {
    service_account = google_service_account.pitchmirror_sa.email

    # gen2 execution environment: better WebSocket keepalive + lower cold-start latency
    execution_environment = "EXECUTION_ENVIRONMENT_GEN2"

    # Session affinity ensures slide uploads and their WS session reach the same
    # instance (in-memory slide decks are per-process, not shared across instances).
    session_affinity = true

    # Keep at least one warm instance: prevents cold-start during demos and ensures
    # the same instance handles upload + WS when session affinity is also set.
    scaling {
      min_instance_count = 1
      max_instance_count = 3
    }

    # Each instance handles few long-lived WebSocket sessions — cap concurrency
    max_instance_request_concurrency = 10

    # Match the asyncio.timeout(900) app-level guard so IaC and code agree
    timeout_seconds = 900

    containers {
      image = var.container_image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "2"
          memory = "1Gi"
        }
      }

      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.project_id
      }

      env {
        name  = "FIRESTORE_COLLECTION"
        value = var.firestore_collection
      }

      env {
        name  = "ENVIRONMENT"
        value = "production"
      }

      env {
        name  = "GEMINI_BACKEND"
        value = "gemini"
      }

      env {
        name = "CORS_ALLOWED_ORIGINS"
        # Explicitly required via variable validation (no synthetic fallback host).
        value = var.allowed_origins
      }

      env {
        name  = "API_BEARER_TOKEN"
        value = var.api_bearer_token
      }

      env {
        name  = "ENABLE_SCREEN_SHARE"
        value = tostring(var.enable_screen_share)
      }

      env {
        name  = "ENABLE_IMAGE_GENERATION"
        value = tostring(var.enable_image_generation)
      }

      env {
        name  = "DEMO_MODE_DEFAULT"
        value = tostring(var.demo_mode_default)
      }

      env {
        name  = "IMAGE_GENERATION_TIMEOUT_S"
        value = tostring(var.image_generation_timeout_s)
      }

      env {
        name  = "IMAGE_GENERATION_RETRIES"
        value = tostring(var.image_generation_retries)
      }

      env {
        name  = "PITCHMIRROR_IMAGE_MODEL"
        value = var.image_model
      }

      env {
        name = "GOOGLE_API_KEY"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.gemini_api_key.secret_id
            version = "latest"
          }
        }
      }
    }
  }

  depends_on = [
    google_project_service.run,
    google_secret_manager_secret.gemini_api_key,
  ]
}

# ── Allow public access to Cloud Run ──────────────────────────────
resource "google_cloud_run_v2_service_iam_member" "public" {
  count    = var.allow_unauthenticated ? 1 : 0
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.pitchmirror.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}
