# AKS Playground

A playground for an Azure DevOps subscription checker and a separate,
Terraform-managed AKS sandbox. Implementation is incremental: this first step
adds state-storage bootstrap tooling only, not AKS provisioning or destruction.

## Repository contents

- `azure-pipelines-checker.yml`: manually triggered subscription checker.
- `config/backend.json`: nonsecret playground state-storage settings.
- `scripts/bootstrap_state.py`: guarded, one-time Azure state-storage bootstrap.
- `tests/test_bootstrap_state.py`: offline tests with faked Azure responses.
- `.gitignore`: excludes credentials, state, plans, and local overrides.
- `README.md`: step-by-step process, usage, and validation status.

The checker does not create, update, or delete Azure resources. The bootstrap
script creates state storage only when explicitly run with `--apply`. Existing
`fitxmatt` resources are not imported or managed by this setup.

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
3. Choose `main` (or a published feature branch you intend to test) and select
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

## Infrastructure lifecycle: step 1 — state storage

### Purpose and boundaries

Terraform's future create/destroy pipelines must share durable state instead of
leaving it on disposable agents. State storage lives in a separate resource group,
outside the future sandbox's Terraform state, so destroying the sandbox will not
destroy its state history. No create/destroy pipeline or Terraform resources are
implemented in this commit.

The committed playground settings are:

| Setting | Value |
| --- | --- |
| Subscription ID | `2c209a53-b9e9-4c5e-8ecf-217c5889ecd7` |
| Tenant ID | `b52fb23a-78d5-4082-b308-dbc1f513bebb` |
| Location | `northeurope` |
| Environment | `aks-sandbox` |
| State resource group | `aks-tfstate-rg` |
| Storage account | `akstfstate2c209a53` |
| Private container | `tfstate` |
| Future state blob | `sandbox/terraform.tfstate` |

Subscription/tenant IDs are identifiers, not credentials. They are committed
deliberately for this playground. Storage account names must be globally unique;
availability has **not** been checked. Edit `config/backend.json` before first
use if the name is taken. For private overrides, copy it to
`config/backend.local.json` and pass `--config config/backend.local.json`.
Do not change the backend location after Terraform starts using it without an
intentional state migration.

### Prerequisites and identity

Use Python 3.9+ and a recent Azure CLI, for example in Azure Cloud Shell. Azure CLI
is not required for the offline preview or tests. Run the following commands from
the repository root.

The human performing bootstrap needs permission to create the resource group and
storage resources and assign Azure roles at their scopes. Subscription Owner
satisfies these Azure RBAC requirements; Contributor alone cannot assign roles.
Tenant MFA and access policies still apply.

The **pipeline identity** gets only **Storage Blob Data Contributor** on the
container from this script. That is a data-plane role; it is separate from the
resource-management and role-assignment permissions the later AKS pipeline needs.
This script does not grant subscription Owner/Contributor to the pipeline.

Find the application used by `azure-service-connection`, then locate its
**Enterprise application / service principal Object ID** in Microsoft Entra ID.
Do not substitute the application (client) ID or the app registration Object ID.
The correct service principal Object ID has not yet been confirmed, so it is a
required runtime argument rather than a guessed committed value.

### Preview, authenticate, and explicitly bootstrap

1. Review `config/backend.json` and preview the intended settings offline:

   ```bash
   python3 scripts/bootstrap_state.py
   ```

   This prints `PREVIEW ONLY`. It makes no Azure calls and does not test access,
   name availability, MFA, or quotas.

2. When you are ready to create state storage, sign in with MFA and select the
   intended subscription. Cloud Shell may already be signed in:

   ```bash
   az login --tenant b52fb23a-78d5-4082-b308-dbc1f513bebb
   az account set --subscription 2c209a53-b9e9-4c5e-8ecf-217c5889ecd7
   ```

3. Read the service principal Object ID interactively, then explicitly apply:

   ```bash
   read -r PIPELINE_PRINCIPAL_OBJECT_ID
   python3 scripts/bootstrap_state.py \
     --apply \
     --confirm-subscription 2c209a53-b9e9-4c5e-8ecf-217c5889ecd7 \
     --pipeline-principal-object-id "$PIPELINE_PRINCIPAL_OBJECT_ID"
   ```

   Paste the nonsecret Object ID at the `read` prompt. The script verifies the
   target subscription, tenant, and enabled status before making changes.
   Every CLI request explicitly targets the configured subscription.

### What bootstrap creates and protects

- A tagged resource group and Standard LRS / StorageV2 account.
- HTTPS-only access, TLS 1.2 minimum, anonymous blob access disabled, and shared
  key authentication disabled. The container is private.
- Blob versioning and 14-day blob/container soft delete. Versions consume storage
  and incur charges; this is recovery assistance, not a backup of AKS workloads.
- A container-scoped role assignment for the provided pipeline identity.

The account has a public network endpoint so Microsoft-hosted agents can reach
it; **this does not allow anonymous access**. Entra authentication and RBAC are
required. A private endpoint/firewall design would need different agent networking.
The script uses management-plane container APIs and never retrieves account keys,
SAS tokens, client secrets, or access tokens.

Existing resource groups/accounts are reused only when their bootstrap ownership
and environment tags match; containers must have matching ownership metadata.
Unrelated resources, incompatible accounts, unavailable names, and failed reads
cause an error instead of silent adoption. Owned resources have security settings
reapplied on reruns; existing matching role assignments are not duplicated.
Do not run multiple bootstrap instances concurrently.

Errors can leave partially created state-storage resources. There is deliberately
no automatic destructive rollback; inspect Azure activity logs, fix the cause,
and rerun with the same configuration. No `fitxmatt` resources are targeted.

### Expected result and backend handoff

On success the script prints the container scope and a propagation reminder.
Check the account, container privacy, versioning/soft-delete settings, and the
pipeline role assignment in Azure portal. RBAC may take several minutes to apply.
Bootstrap does **not** create a Terraform state blob; Terraform writes it later.

Generate the future backend's noncredential settings from the same configuration:

```bash
python3 scripts/bootstrap_state.py --print-backend-config > config/backend.local.hcl
```

The generated file is ignored. It enables Entra data-plane authentication and
native OIDC; later commits will add the Terraform backend block and federated
pipeline identity/token configuration. It is not a complete standalone Terraform
setup yet. Blob leases provide Terraform state locking once that backend is used.
Never commit state or saved plans: they may contain sensitive values.

### Local validation and next steps

Run offline tests without Azure credentials or third-party Python packages:

```bash
python3 -m unittest discover -s tests -v
```

Tests cover configuration validation, subscription/tenant checks, explicit apply
guards, security flags, ownership/collision checks, resumable bootstrap, scoped
RBAC, and credential-free CLI construction using faked responses.
These tests do **not** establish that Azure permissions or names are valid.

Local validation: all 21 offline unit tests passed, preview/backend rendering
worked, and whitespace checks passed. No Azure bootstrap or pipeline run was
performed, and no storage-name availability check was made. Remaining manual
actions: confirm the pipeline principal Object ID, review permissions, and run
bootstrap when authorized.

Subsequent commits will add the Terraform sandbox, shared pipeline tooling,
manual creation pipeline, guarded destruction pipeline, and the final lifecycle
walkthrough. Each will update this README with its own usage and validation.
