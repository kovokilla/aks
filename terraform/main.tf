resource "azurerm_resource_group" "sandbox" {
  name     = var.resource_group_name
  location = local.backend_settings.location
  tags     = local.tags

  lifecycle {
    precondition {
      condition = (
        var.resource_group_name != local.backend_settings.resource_group_name &&
        var.node_resource_group_name != local.backend_settings.resource_group_name &&
        var.node_resource_group_name != var.resource_group_name
      )
      error_message = "Sandbox, AKS node, and backend resource groups must all be different."
    }
  }
}

resource "azurerm_container_registry" "sandbox" {
  name                = var.acr_name
  resource_group_name = azurerm_resource_group.sandbox.name
  location            = azurerm_resource_group.sandbox.location
  sku                 = "Basic"
  admin_enabled       = false
  tags                = local.tags
}

resource "azurerm_kubernetes_cluster" "sandbox" {
  name                = var.cluster_name
  resource_group_name = azurerm_resource_group.sandbox.name
  location            = azurerm_resource_group.sandbox.location
  dns_prefix          = var.cluster_name
  node_resource_group = var.node_resource_group_name
  kubernetes_version  = var.kubernetes_version
  sku_tier            = "Free"

  role_based_access_control_enabled = true
  local_account_disabled            = true

  azure_active_directory_role_based_access_control {
    tenant_id          = local.backend_settings.tenant_id
    azure_rbac_enabled = true
  }

  default_node_pool {
    name                 = "system"
    type                 = "VirtualMachineScaleSets"
    node_count           = var.node_count
    vm_size              = var.node_vm_size
    os_sku               = "Ubuntu"
    os_disk_size_gb      = 64
    auto_scaling_enabled = false
  }

  identity {
    type = "SystemAssigned"
  }

  network_profile {
    network_plugin      = "azure"
    network_plugin_mode = "overlay"
    pod_cidr            = "10.244.0.0/16"
    service_cidr        = "10.100.0.0/16"
    dns_service_ip      = "10.100.0.10"
    load_balancer_sku   = "standard"
    outbound_type       = "loadBalancer"

    load_balancer_profile {
      managed_outbound_ip_count = 1
    }
  }

  tags = local.tags
}

resource "azurerm_role_assignment" "acr_pull" {
  scope                            = azurerm_container_registry.sandbox.id
  role_definition_name             = "AcrPull"
  principal_id                     = azurerm_kubernetes_cluster.sandbox.kubelet_identity[0].object_id
  skip_service_principal_aad_check = true
}
