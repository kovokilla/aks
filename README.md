# Azure AKS Cluster Infrastructure (Azure DevOps CI/CD)

A complete Terraform-based infrastructure for deploying applications to Azure Kubernetes Service with Azure DevOps for CI/CD.

## Architecture

```
GitHub Repo (Source Only)
    │
    ├── Azure DevOps Builder (CI)
    │       └── Build Docker Image → Azure Container Registry
    │
    ├── Azure DevOps Release (CD)  
    │       └── Deploy to AKS
    │
    └── Terraform (Infrastructure)
            └── AKS Cluster in Azure
```

## Prerequisites

1. **Azure Subscription** - With contributor rights
2. **Terraform 1.8+** - Installed locally
3. **Azure CLI** - Logged in (`az login`)
4. **kubectl** - For managing AKS cluster
5. **GitHub Account** - For source control
6. **Azure DevOps Organization** - For CI/CD pipelines

## Quick Start

### 1. Deploy Infrastructure (Terraform)

```bash
# Clone the repo
git clone https://github.com/kovokilla/aks.git
cd aks

# Initialize Terraform
cd terraform
terraform init

# Customize variables (optional)
cat > terraform.tfvars << EOF
resource_group_name = "aks-production-rg"
location            = "West Europe"
aks_cluster_name    = "aks-production"
node_count          = 3
kubernetes_version  = "1.29"
EOF

# Plan
terraform plan -out=tfplan

# Apply
terraform apply tfplan

# Get credentials
az aks get-credentials --resource-group aks-production-rg --name aks-production --overwrite-existing
```

### 2. Configure Azure DevOps

1. Create Azure DevOps organization and project
2. Create Service Connections:
   - **Azure Resource Manager** - For Terraform deployment
   - **Azure Kubernetes** - For AKS deployment  
   - **Docker Registry** - For ACR (if using Azure Container Registry)

3. Create Azure Repos pipeline using `azure-pipelines.yml`

4. Set up pipeline triggers on GitHub commits

## File Structure

```
azure-aks-demo/
├── terraform/
│   ├── providers.tf        # Azure provider configuration
│   ├── variables.tf        # Infrastructure variables
│   ├── main.tf             # AKS cluster and resources
│   └── outputs.tf          # Cluster outputs/credentials
│
├── k8s/
│   ├── namespace.yaml      # Kubernetes namespace
│   ├── deployment.yaml     # App deployment
│   ├── service.yaml        # LoadBalancer service
│   └── ingress.yaml        # Ingress controller config
│
├── app/
│   ├── Dockerfile          # Docker image definition
│   ├── server.js           # Sample Node.js application
│   └── package.json        # Node.js dependencies
│
├── azure-pipelines.yml     # Azure DevOps build pipeline
├── azure-pipelines-deploy.yml  # Azure DevOps deploy pipeline
└── README.md               # This file
```

## Azure DevOps Setup

### Subscription Checker Pipeline

To verify the Azure service connection without deploying resources:

1. In Azure DevOps, create a pipeline using this GitHub repository.
2. Select **Existing Azure Pipelines YAML file**, choose branch
   `feat/pipe-checker`, and select `/azure-pipelines-checker.yml`.
3. Ensure an Azure Resource Manager service connection named
   `azure-service-connection` exists and authorize this pipeline to use it.
   If your connection has a different name, update `azureSubscription` in the YAML.
4. Run the pipeline manually. The **Print Azure subscription ID** step outputs
   the active subscription ID associated with the service connection.

The checker has no automatic push or pull-request triggers, does not require
the `aks-secrets` variable group, and does not create or modify Azure resources.

### Variables Group: `aks-secrets`

| Variable | Description |
|----------|-------------|
| AZURE_SUBSCRIPTION_ID | Your Azure subscription ID |
| RESOURCE_GROUP_NAME | AKS resource group name |
| AKS_CLUSTER_NAME | AKS cluster name |
| STATE_STORAGE_ACCOUNT | Terraform state storage |
| CONTAINER_REGISTRY_NAME | Azure Container Registry name |

### Pipeline Triggers

```yaml
trigger:
  branches:
    include:
      - main
      - develop
```

## Post-Deployment

### Access Application

```bash
# Get the external IP
kubectl get service app-service -n production

# Access the app
curl http://<EXTERNAL_IP>
```

### Monitor Cluster

```bash
# Check nodes
kubectl get nodes

# Check pods
kubectl get pods -n production

# View logs
kubectl logs -l app=azure-aks-demo -n production --tail=100
```

## Cleanup

```bash
# Delete Kubernetes resources
kubectl delete namespace production

# Destroy infrastructure
cd terraform
terraform destroy
```

## Configuration Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `resource_group_name` | aks-cluster-rg | Azure Resource Group |
| `location` | West Europe | Azure Region |
| `aks_cluster_name` | aks-cluster | Cluster Name |
| `kubernetes_version` | 1.29 | Kubernetes version |
| `node_count` | 3 | Initial node count |
| `node_vm_size` | Standard_D4s_v3 | Node VM size |

## Cost Estimation

- **AKS Cluster**: ~$200-400/month (3 × Standard_D4s_v3)
- **Log Analytics**: ~$30/month
- **Storage**: ~$10/month
- **Total**: ~$240-440/month

## Security Features

- ✅ RBAC enabled
- ✅ System-assigned managed identity
- ✅ Network policy (Azure CNI)
- ✅ AAD integration ready
- ✅ Monitoring via Azure Monitor
- ✅ TLS enabled (Ingress)

## Troubleshooting

**RBAC permission errors:**
```bash
az aks get-credentials --resource-group <rg> --name <cluster> --admin
```

**Image pull errors:**
- Check image name in deployment.yaml
- Verify Azure Container Registry permissions
- Ensure AZURE_CLIENT_ID/AZURE_CLIENT_SECRET are set

**Ingress controller not ready:**
```bash
kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/main/deploy/static/provider/cloud/azure/deploy.yaml
```

## Next Steps

1. Enable Azure Monitor for containers
2. Set up Azure Key Vault for secrets
3. Implement Horizontal Pod Autoscaler
4. Add Azure Front Door for global traffic
5. Enable multi-region deployment for HA