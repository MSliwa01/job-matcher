variable "image_tag" {
  type        = string
  default     = "latest"
  description = "Tag pushed to the ECR repository"
}

variable "budget_email" {
  type        = string
  description = "Where the budget alerts go"
}

variable "name" {
  type    = string
  default = "job-matcher"
}

variable "region" {
  type    = string
  default = "eu-central-1"
}

variable "reserved_concurrency" {
  type    = number
  default = 2
}

variable "budget_usd" {
  type    = number
  default = 1
}
