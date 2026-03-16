variable "project_id" {
  description = "Google Cloud project ID"
  type        = string
}

variable "region" {
  description = "Google Cloud region for Cloud Run and Firestore"
  type        = string
  default     = "us-central1"
}

variable "service_name" {
  description = "Cloud Run service name"
  type        = string
  default     = "pitchmirror"
}

variable "firestore_collection" {
  description = "Firestore collection for persisted session scorecards."
  type        = string
  default     = "pitchmirror_sessions"
}

variable "gemini_secret_id" {
  description = "Secret Manager secret ID that stores the Gemini API key."
  type        = string
  default     = "pitchmirror-gemini-api-key"
}

variable "container_image" {
  description = "Full container image URI (e.g. gcr.io/PROJECT/pitchmirror:latest)"
  type        = string
}

variable "allowed_origins" {
  description = "Comma-separated list of allowed CORS origins (e.g. https://pitchmirror.example.com). Required to avoid wildcard CORS in production."
  type        = string

  validation {
    condition     = length(trimspace(var.allowed_origins)) > 0
    error_message = "allowed_origins must be set (no empty fallback)."
  }
}

variable "allow_unauthenticated" {
  description = "If true, grant allUsers Cloud Run invoker role. Default is locked down."
  type        = bool
  default     = false
}

variable "api_bearer_token" {
  description = "Shared token enforced by backend for /api and /ws."
  type        = string
  sensitive   = true

  validation {
    condition     = length(trimspace(var.api_bearer_token)) >= 24
    error_message = "api_bearer_token must be at least 24 characters."
  }
}

variable "enable_screen_share" {
  description = "Enable optional screen-share ingestion for slide-aware coaching."
  type        = bool
  default     = true
}

variable "enable_image_generation" {
  description = "Enable post-session multimodal image generation."
  type        = bool
  default     = true
}

variable "demo_mode_default" {
  description = "Default deterministic demo mode if the client does not specify one."
  type        = bool
  default     = false
}

variable "image_generation_timeout_s" {
  description = "Timeout per generated image (seconds)."
  type        = number
  default     = 24
}

variable "image_generation_retries" {
  description = "Retry count per generated image."
  type        = number
  default     = 1
}

variable "image_model" {
  description = "Image model used for multimodal visual outputs."
  type        = string
  default     = "imagen-4.0-fast-generate-001"
}
