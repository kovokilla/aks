output "resource_group_name" {
  description = "Sandbox resource group, not the persistent state group."
  value       = azurerm_resource_group.sandbox.name
}

output "cluster_name" {
  value = azurerm_kubernetes_cluster.sandbox.name
}

output "node_resource_group_name" {
  value = azurerm_kubernetes_cluster.sandbox.node_resource_group
}

output "acr_login_server" {
  value = azurerm_container_registry.sandbox.login_server
}
