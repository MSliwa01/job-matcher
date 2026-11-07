variable "subscription_id" {
  type        = string
  description = "Azure subscription id (az account show --query id -o tsv)"
}

variable "image" {
  type        = string
  description = "Public container image, e.g. ghcr.io/<user>/job-matcher-demo:<tag>"
}

variable "budget_email" {
  type        = string
  description = "Where the budget alerts go"
}

variable "name" {
  type    = string
  default = "job-matcher"
}

variable "location" {
  type    = string
  default = "polandcentral"
}

variable "budget_usd" {
  type        = number
  default     = 2.5
  description = "Monthly budget (about 10 PLN). Email at 40%, app stopped at 100%."
}
