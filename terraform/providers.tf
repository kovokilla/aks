locals {
  backend_settings = jsondecode(file("${path.module}/../config/backend.json"))
  tags = {
    environment = local.backend_settings.environment
    managed-by  = "terraform"
    purpose     = "aks-playground"
  }
}

provider "azurerm" {
  subscription_id = local.backend_settings.subscription_id
  tenant_id       = local.backend_settings.tenant_id

  # Even plan must not auto-register Azure providers as a side effect.
  resource_provider_registrations = "none"

  features {
    resource_group {
      prevent_deletion_if_contains_resources = true
    }
  }
}
