# Azure Subscription Checker

A minimal Azure DevOps pipeline that verifies Azure authentication and prints
the subscription ID associated with an Azure Resource Manager service connection.

## Repository contents

- `azure-pipelines-checker.yml`: manually triggered subscription checker.
- `README.md`: setup, usage, and troubleshooting instructions.

There is no AKS deployment, Terraform infrastructure, application, or image build
in this repository. The checker does not create, update, or delete Azure resources.

## Prerequisites

- An Azure DevOps project with access to this GitHub repository.
- An Azure subscription and an identity authorized to access it.
- An **Azure Resource Manager** service connection named
  `azure-service-connection` in the same Azure DevOps project.
- Access to a Microsoft-hosted `ubuntu-latest` agent and available parallel-job
  capacity.

A GitHub service connection (such as `github.com_kovokilla`) provides repository
access only. It cannot replace the Azure Resource Manager connection.

## Configure the Azure connection

1. Open **Project settings → Service connections → New service connection**.
2. Select **Azure Resource Manager** and use **Workload identity federation**.
3. Use automatic app registration if permitted, or a manually configured app
   registration with a federated credential.
4. Select the intended subscription and name the connection
   `azure-service-connection`. If using another name, update `azureSubscription`
   in the pipeline YAML to match.
5. Under **Pipeline permissions**, authorize only the checker pipeline rather
   than granting access to all pipelines.

The checker does not require Contributor access; Reader at the selected scope
is sufficient. Creating the connection automatically may require additional
permissions to register applications and assign Azure roles. Those setup
permissions are separate from the pipeline identity's runtime permissions.
Complete MFA when Azure requires it during connection setup. No client secret
is needed for workload identity federation.

## Create and run the pipeline

1. In Azure DevOps, select **Pipelines → New pipeline → GitHub** and choose
   `kovokilla/aks`.
2. Select **Existing Azure Pipelines YAML file**.
3. Choose the branch containing this checker-only setup (`chore/checker-only`
   once pushed, or `main` after merging) and select
   `/azure-pipelines-checker.yml`.
4. Save the pipeline and select **Run pipeline**.
5. If the run waits for resource access, use **View → Permit** to authorize
   the Azure service connection, or ask its administrator to grant access.

Both push and pull-request triggers are disabled. Run the checker manually;
no variable group, local Azure CLI installation, or local Azure sign-in is needed.

## Expected output

Open the **Print Azure subscription ID** step in the run logs. The task signs in
using the service connection and executes:

```bash
az account show --query id --output tsv
```

The command prints the active subscription ID as a UUID. Compare it with the
subscription ID in Azure portal to confirm the connection targets the intended
subscription. Azure CLI task setup may print additional log messages.

A successful run verifies authentication and subscription selection only. It does
not validate AKS access, resource deployment permissions, or infrastructure.

## Troubleshooting

- **Service connection missing or unauthorized:** check the exact connection
  name, its Azure Resource Manager type, and its pipeline permissions in this
  project.
- **Run waiting for resource permission:** authorize this pipeline to use the
  connection; authorization is separate from Azure role assignments.
- **Variable group not found:** confirm the pipeline loads
  `/azure-pipelines-checker.yml`; this checker uses no variable groups.
- **MFA required during connection creation:** authenticate the account creating
  the connection with MFA, then retry. Manual setup does not bypass MFA rules.
- **Federation/sign-in failure:** verify the tenant ID, application (client) ID,
  and the app registration's federated credential against the issuer, subject,
  and audience supplied by Azure DevOps.
- **Unexpected subscription ID:** inspect the service connection's configured
  subscription, rather than changing the GitHub connection.
