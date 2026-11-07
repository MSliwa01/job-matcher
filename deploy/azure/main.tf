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

# Cost guard. Budgets only send alerts, so a Logic App stops the container app when the budget is spent:
# an email at 40% of the budget and on a forecast above it, the stop at 100% of actual cost. Azure
# updates cost data a few times a day, so the stop comes hours after the money is spent, not seconds.
# `az containerapp start` (or `terraform apply` after a new revision) brings it back.
resource "azurerm_logic_app_workflow" "kill" {
  name                = "${var.name}-kill"
  location            = azurerm_resource_group.rg.location
  resource_group_name = azurerm_resource_group.rg.name
  identity {
    type = "SystemAssigned"
  }
}

resource "azurerm_logic_app_trigger_http_request" "budget" {
  name         = "budget-alert"
  logic_app_id = azurerm_logic_app_workflow.kill.id
  schema       = "{}"
}

resource "azurerm_logic_app_action_custom" "stop" {
  name         = "stop-container-app"
  logic_app_id = azurerm_logic_app_workflow.kill.id
  body = jsonencode({
    type     = "Http"
    runAfter = {}
    inputs = {
      method = "POST"
      uri    = "https://management.azure.com${azurerm_container_app.api.id}/stop?api-version=2024-03-01"
      authentication = {
        type     = "ManagedServiceIdentity"
        audience = "https://management.azure.com/"
      }
    }
  })
}

resource "azurerm_role_assignment" "kill" {
  scope                = azurerm_container_app.api.id
  role_definition_name = "Contributor"
  principal_id         = azurerm_logic_app_workflow.kill.identity[0].principal_id
}

resource "azurerm_monitor_action_group" "kill" {
  name                = "${var.name}-kill"
  resource_group_name = azurerm_resource_group.rg.name
  short_name          = "jmkill"

  logic_app_receiver {
    name                    = "stop-app"
    resource_id             = azurerm_logic_app_workflow.kill.id
    callback_url            = azurerm_logic_app_trigger_http_request.budget.callback_url
    use_common_alert_schema = true
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
    threshold      = 40
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

  notification {
    enabled        = true
    threshold      = 100
    operator       = "GreaterThan"
    threshold_type = "Actual"
    contact_emails = [var.budget_email]
    contact_groups = [azurerm_monitor_action_group.kill.id]
  }

  lifecycle {
    ignore_changes = [time_period]
  }
}

output "url" {
  value = "https://${azurerm_container_app.api.ingress[0].fqdn}"
}
