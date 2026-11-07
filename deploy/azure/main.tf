# The demo on Azure Container Apps, scaled to zero when idle. The consumption plan's monthly free grant
# (180,000 vCPU-seconds, 360,000 GiB-seconds, 2 million requests) covers a demo's traffic.
# No Log Analytics workspace, so log storage costs nothing; `az containerapp logs show` still streams logs.

terraform {
  required_version = ">= 1.6"
  required_providers {
    azurerm = { source = "hashicorp/azurerm", version = "~> 4.0" }
  }
}

provider "azurerm" {
  features {}
  subscription_id = var.subscription_id
}

resource "azurerm_resource_group" "rg" {
  name     = "${var.name}-rg"
  location = var.location
}

resource "azurerm_container_app_environment" "env" {
  name                = "${var.name}-env"
  location            = azurerm_resource_group.rg.location
  resource_group_name = azurerm_resource_group.rg.name
}

resource "azurerm_container_app" "api" {
  name                         = var.name
  container_app_environment_id = azurerm_container_app_environment.env.id
  resource_group_name          = azurerm_resource_group.rg.name
  revision_mode                = "Single"

  template {
    min_replicas = 0 # scale to zero: no requests, no charge
    max_replicas = 1 # a ceiling on the bill if someone floods the endpoint

    container {
      name   = "api"
      image  = var.image
      cpu    = 1
      memory = "2Gi" # the int8 model and the snapshot take about 1.1 GiB

      readiness_probe {
        transport = "HTTP"
        port      = 8080
        path      = "/healthz"
      }
    }

    http_scale_rule {
      name                = "http"
      concurrent_requests = "20"
    }
  }

  ingress {
    external_enabled = true
    target_port      = 8080
    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }
}

resource "azurerm_consumption_budget_subscription" "budget" {
  name            = "${var.name}-budget"
  subscription_id = "/subscriptions/${var.subscription_id}"
  amount          = var.budget_usd
  time_grain      = "Monthly"

  time_period {
    start_date = formatdate("YYYY-MM-01'T'00:00:00Z", timestamp())
  }

  notification {
    enabled        = true
    threshold      = 50
    operator       = "GreaterThan"
    threshold_type = "Actual"
    contact_emails = [var.budget_email]
  }

  notification {
    enabled        = true
    threshold      = 100
    operator       = "GreaterThan"
    threshold_type = "Forecasted"
    contact_emails = [var.budget_email]
  }

  lifecycle {
    ignore_changes = [time_period]
  }
}

output "url" {
  value = "https://${azurerm_container_app.api.ingress[0].fqdn}"
}
