mock_provider "azurerm" {
  override_during = plan
}

override_resource {
  override_during = plan
  target          = azurerm_container_registry.sandbox
  values = {
    id           = "/subscriptions/11111111-1111-4111-8111-111111111111/resourceGroups/aks-sandbox-rg/providers/Microsoft.ContainerRegistry/registries/akssandbox2c209a53"
    login_server = "akssandbox2c209a53.azurecr.io"
  }
}


run "sandbox_plan" {
  command = plan

  # AzureRM synthesizes kubelet_identity through provider planning logic that
  # generic mocks do not execute. Validate RBAC with a real Azure plan later.
  plan_options {
    target = [
      azurerm_resource_group.sandbox,
      azurerm_container_registry.sandbox,
      azurerm_kubernetes_cluster.sandbox,
    ]
  }

  assert {
    condition     = azurerm_resource_group.sandbox.name == "aks-sandbox-rg"
    error_message = "The default plan must use a separate sandbox."
  }

  assert {
    condition     = azurerm_kubernetes_cluster.sandbox.default_node_pool[0].type == "VirtualMachineScaleSets"
    error_message = "The AKS system pool must be VMSS-backed."
  }

  assert {
    condition     = azurerm_container_registry.sandbox.admin_enabled == false
    error_message = "ACR admin credentials must remain disabled."
  }


  assert {
    condition     = azurerm_kubernetes_cluster.sandbox.local_account_disabled
    error_message = "AKS local admin accounts must be disabled."
  }
}

run "reject_existing_environment" {
  command = plan
  plan_options {
    target = [azurerm_resource_group.sandbox]
  }
  variables {
    resource_group_name = "fitxmatt-rg"
  }
  expect_failures = [var.resource_group_name]
}

run "reject_zero_nodes" {
  command = plan
  plan_options {
    target = [azurerm_kubernetes_cluster.sandbox]
  }
  variables {
    node_count = 0
  }
  expect_failures = [var.node_count]
}

run "reject_backend_group_collision" {
  command = plan
  plan_options {
    target = [azurerm_resource_group.sandbox]
  }
  variables {
    resource_group_name = "aks-tfstate-rg"
  }
  expect_failures = [var.resource_group_name]
}

run "reject_node_group_collision" {
  command = plan
  plan_options {
    target = [azurerm_resource_group.sandbox]
  }
  variables {
    node_resource_group_name = "aks-sandbox-rg"
  }
  expect_failures = [azurerm_resource_group.sandbox]
}
