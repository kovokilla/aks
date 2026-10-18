variable "resource_group_name" {
  description = "Separate sandbox resource group; never the existing fitxmatt or state group."
  type        = string

  validation {
    condition     = can(regex("^aks-sandbox-[a-z0-9-]+$", var.resource_group_name))
    error_message = "Sandbox resource group names must start with aks-sandbox-."
  }
}

variable "cluster_name" {
  description = "Name of the new sandbox AKS cluster."
  type        = string

  validation {
    condition     = can(regex("^aks-sandbox([a-z0-9-]*)$", var.cluster_name))
    error_message = "Cluster names must start with aks-sandbox."
  }
}

variable "node_resource_group_name" {
  description = "Dedicated node resource group created and managed by AKS."
  type        = string

  validation {
    condition     = can(regex("^aks-sandbox-[a-z0-9-]+$", var.node_resource_group_name))
    error_message = "Node resource group names must start with aks-sandbox-."
  }
}

variable "acr_name" {
  description = "Globally unique sandbox registry name; availability is checked by Azure."
  type        = string

  validation {
    condition     = can(regex("^akssandbox[a-z0-9]{0,40}$", var.acr_name))
    error_message = "Registry names must start with akssandbox and contain only lowercase letters/digits."
  }
}

variable "node_count" {
  description = "Fixed node count for the VMSS-backed system pool; not autoscaling."
  type        = number

  validation {
    condition     = var.node_count >= 1 && var.node_count <= 3 && floor(var.node_count) == var.node_count
    error_message = "Use an integer node count between 1 and 3."
  }
}

variable "node_vm_size" {
  description = "AKS system-node SKU; availability and quota must be checked in the target region."
  type        = string

  validation {
    condition     = can(regex("^Standard_[A-Za-z0-9_]+$", var.node_vm_size))
    error_message = "Use a Standard Azure VM SKU."
  }
}

variable "kubernetes_version" {
  description = "Null selects Azure's recommended version on creation; use a supported explicit version to pin it."
  type        = string
  default     = null

  validation {
    condition     = var.kubernetes_version == null ? true : can(regex("^1\\.[0-9]+(\\.[0-9]+)?$", var.kubernetes_version))
    error_message = "Use null or a Kubernetes minor/patch version such as 1.xx or 1.xx.y."
  }
}
