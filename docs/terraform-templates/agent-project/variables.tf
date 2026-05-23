variable "project_id" {
  description = "GCP Project ID for the agent (must be globally unique)"
  type        = string
}

variable "project_name" {
  description = "Human-readable project name"
  type        = string
}

variable "organization_id" {
  description = "GCP Organization ID"
  type        = string
}

variable "billing_account" {
  description = "GCP Billing Account ID"
  type        = string
}

variable "region" {
  description = "Default region for resources"
  type        = string
  default     = "us-central1"
}

variable "bot_name" {
  description = "Display name for the agent/bot (used across platforms)"
  type        = string
}

variable "bot_account_id" {
  description = "Service account ID base (lowercase, hyphens only, max 30 chars). Used for service accounts and secret names."
  type        = string
}

variable "bot_description" {
  description = "Description of what the agent/bot does (used for Google Chat configuration)"
  type        = string
}

variable "bot_avatar_url" {
  description = "URL for the bot's avatar image (used for Google Chat, optional)"
  type        = string
  default     = ""
}

variable "secret_name" {
  description = "Name for the Google Chat service account secret in Secret Manager"
  type        = string
}

variable "middleware_project_id" {
  description = "The GCP project ID where the middleware is deployed (for IAM bindings)"
  type        = string
  default     = "vertex-ai-middleware-prod"
}

variable "discord_application_id" {
  description = "Discord application ID from the Developer Portal (General Information → Application ID). Required only if the Discord secret is populated; register_agent.py writes it onto the Firestore platform block for traceability. Leave empty if not using Discord."
  type        = string
  default     = ""
}
