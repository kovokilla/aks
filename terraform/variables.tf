variable "resource_group_name" {
  description = "Name of the resource group"
  type        = string
  default     = "aks-cluster-rg"
}

variable "location" {
  description = "Azure region for resources"
  type        = string
  default     = "West Europe"
}

variable "aks_cluster_name" {
  description = "Name of the AKS cluster"
  type        = string
  default     = "aks-cluster"
}

variable "kubernetes_version" {
  description = "Kubernetes version for AKS"
  type        = string
  default     = "1.29"
}

variable "node_count" {
  description = "Number of nodes in the AKS pool"
  type        = number
  default     = 3
}

variable "node_vm_size" {
  description = "VM size for AKS nodes"
  type        = string
  default     = "Standard_D4s_v3"
}

variable "dns_prefix" {
  description = "DNS prefix for the AKS cluster"
  type        = string
  default     = "aksdemo"
}

variable "ssh_public_key" {
  description = "SSH public key for Linux nodes"
  type        = string
  default     = "~/.ssh/id_rsa.pub"
}