# AKS Playground

A playground for an Azure DevOps subscription checker and a separate,
Terraform-managed AKS sandbox. State-storage bootstrap and sandbox Terraform
configuration and a manually triggered creation pipeline are implemented.
The separate destruction pipeline is not yet added.

## Repository contents

- `azure-pipelines-checker.yml`: manually triggered subscription checker.
- `azure-pipelines-create.yml`: install/check Terraform, plan, review, and
  optionally provision the sandbox.
- `pipelines/templates/terraform-job-steps.yml`: shared deployment-job steps.
- `config/backend.json`: nonsecret playground state-storage settings.
- `scripts/bootstrap_state.py`: guarded, one-time Azure state-storage bootstrap.
- `scripts/init_backend.py`: initialize the Azure backend with a human CLI login.
- `scripts/install_terraform.py`: checksum-verified pinned Terraform installation.
- `scripts/check_pipeline_environment.py`: read-only private-project and sandbox
  context validation; native Azure DevOps controls enforce environment gates.
- `scripts/pipeline_terraform.py`: guarded native-OIDC runner for saved-plan
  creation/application by Azure DevOps.
- `terraform/`: sandbox resources, remote backend declaration, inputs, outputs,
  signed provider lockfile, and targeted mock-plan tests.
- `tests/`: offline bootstrap, backend initialization, and installer unit tests.
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
destroy its state history. The sandbox configuration introduced in step 2 does
not declare or import these persistent storage resources.

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
Resolve the service principal from the service connection's application (client)
ID. The confirmed playground application ID is
`0b9718f6-7c6e-43de-9e36-d18455c1e8b3`:

```bash
az ad sp show \
  --id 0b9718f6-7c6e-43de-9e36-d18455c1e8b3 \
  --query '{name:displayName,applicationId:appId,principalObjectId:id}' \
  --output json
```

Use `principalObjectId` for the bootstrap argument. It remains a runtime value,
not a guessed committed default. Do not use the app registration Object ID
`6a8ab55b-33be-4271-861e-f12b7ae132ca`: Azure identified that object as an
Application, which cannot receive Azure RBAC assignments.

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

3. Resolve the correct service principal Object ID and explicitly apply:

   ```bash
   PIPELINE_PRINCIPAL_OBJECT_ID=$(az ad sp show \
     --id 0b9718f6-7c6e-43de-9e36-d18455c1e8b3 \
     --query id --output tsv) &&
   python3 scripts/bootstrap_state.py \
     --apply \
     --confirm-subscription 2c209a53-b9e9-4c5e-8ecf-217c5889ecd7 \
     --pipeline-principal-object-id "$PIPELINE_PRINCIPAL_OBJECT_ID"
   ```

   The `&&` prevents bootstrap if the lookup fails; the script also rejects an
   empty or invalid Object ID. It verifies the target subscription, tenant,
   and enabled status before making changes.
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

### Issues found and fixed during Cloud Shell testing

- Azure CLI rejects combining `role assignment list --scope` with `--all`.
  Listing now uses the container scope without `--all`, and disables unnecessary
  principal-name lookups through Microsoft Graph.
- Container list responses omitted ownership metadata even though the container
  had it. Reruns now read the full container with `container-rm show` before
  checking ownership. The ownership guard is preserved.
- `PrincipalTypeNotSupported` identified an app registration Object ID passed
  instead of its service principal Object ID. Resolve the Enterprise application
  from its application (client) ID as shown above; this is not an MFA failure.
- Known Azure error codes now have safe diagnostic hints in the wrapper.
  Raw CLI stdout/stderr, credentials, and tokens are not dumped.

Regression tests cover the CLI argument combination and list/detail response
difference. On an older Cloud Shell checkout with manual edits, do not blindly
discard them or pull over them: compare changes with Git first. A fresh clone of
the published branch includes these fixes without manual patches.

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
native OIDC for pipeline use. The backend declaration is in
`terraform/backend.tf`; the shared pipeline runner supplies federated identity
and token-request configuration.
For a human Cloud Shell login, use the CLI-authenticated initializer in step 2
instead. Blob leases provide state locking once the backend is used.
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

Bootstrap follow-up validation: all 24 then-existing unit tests passed,
preview/backend rendering worked, and whitespace checks passed.
The subscription checker is unchanged.

### Live validation record — 2026-10-17

The operator ran bootstrap in Azure Cloud Shell against the configured
subscription/tenant, applied the CLI/metadata fixes above, resolved the service
principal correctly, and reported successful completion:

```text
State storage configured. Container scope: /subscriptions/2c209a53-b9e9-4c5e-8ecf-217c5889ecd7/resourceGroups/aks-tfstate-rg/providers/Microsoft.Storage/storageAccounts/akstfstate2c209a53/blobServices/default/containers/tfstate
Terraform creates the state blob on its first state write. Allow time for RBAC propagation.
```

A read-only container query supplied by the operator also showed
`publicAccess: None` and matching `managed_by: aks-terraform-bootstrap` /
`environment: aks-sandbox` metadata. Successful bootstrap means its storage
configuration and role-assignment commands completed without an error.
This evidence was supplied from Cloud Shell, not independently collected from
the local development machine.

The operator subsequently supplied account/blob-service readbacks confirming:
HTTPS-only `true`, minimum TLS `TLS1_2`, public blob access `false`, shared-key
authentication `false`, versioning `true`, and enabled 14-day blob/container soft
delete. This confirms the storage configuration through operator-provided evidence.

Subsequent pipeline backend initialization, plan, and normal state-lock operation
succeeded on 2026-10-18 using the pipeline identity; see the step 4 live record.
Human Owner access alone would not have proved pipeline data-plane access.
No apply, destroy, state recovery, or deployed sandbox has yet been verified.

Shared pipeline tooling and the creation pipeline are documented below.
The guarded destruction pipeline remains a separate future step. Each commit
updates this README with its own usage and validation.

## Infrastructure lifecycle: step 2 — Terraform configuration and backend access

### What is defined, but not deployed

The root module in `terraform/` defines four managed resources:

1. `aks-sandbox-rg`: separate sandbox resource group in North Europe.
2. `akssandbox2c209a53`: Basic ACR with admin credentials disabled.
3. `aks-sandbox`: Free-tier AKS control plane with a VMSS-backed `system` pool,
   one `Standard_D2s_v5` node, Azure CNI overlay, managed identity, and Entra/Azure
   RBAC with local accounts disabled. Node count is fixed, not autoscaled.
4. An `AcrPull` assignment for the kubelet identity scoped only to the new ACR.

AKS creates/manages `aks-sandbox-nodes-rg` and its VMSS, networking, identity, and
outbound public IP. Terraform does not independently manage those generated
resources, the shared Network Watcher, or any existing `fitxmatt` resources.
Changing IP addresses during recreation is expected. There are no application
deployments, images, certificates, or workload backups.

`terraform/sandbox.auto.tfvars.json` is the common source of sandbox names and
sizing. `config/backend.json` supplies the subscription, tenant, environment,
location, and backend settings. The provider reads that committed backend JSON;
a bootstrap-only local override does not automatically retarget the sandbox.
For a different environment, update these sources together and review state
migration separately. Guardrails reject non-sandbox names and colliding groups.

Terraform is pinned to **1.13.3** and AzureRM to **4.48.0**, with provider checksums
locked for Linux amd64 and macOS arm64. These are fixed, validated versions, not
a claim to be the newest releases. Kubernetes defaults to Azure's recommended
version at creation rather than an obsolete fixed version; pin a supported
version in the input file if reproducibility across different dates is required.

No automatic resource-provider registration is performed, even during plan.
Before a future apply, an administrator must ensure `Microsoft.ContainerService`,
`Microsoft.ContainerRegistry`, `Microsoft.Compute`, `Microsoft.Network`, and
`Microsoft.ManagedIdentity` are registered. VM quota, SKU/version availability,
ACR name availability, and Azure policies still require a real Azure check.
Deployment requires resource-creation permissions and permission to assign
`AcrPull`; Reader-only checker access is insufficient. Kubernetes workload access
requires separate AKS Azure RBAC assignments.

### Initialize the backend in Cloud Shell without applying

Use a fresh checkout of `feat/terraform-lifecycle` until it is merged. Earlier
Cloud Shell checkouts may have manual patches: preserve them instead of using
`git reset --hard` or overwriting files.

From the repository root, install the pinned Terraform into a temporary directory:

```bash
TF_TOOLS_DIR=$(mktemp -d)
python3 scripts/install_terraform.py --directory "$TF_TOOLS_DIR" &&
export PATH="$TF_TOOLS_DIR:$PATH"
terraform version
```

The installer downloads only the fixed version from HashiCorp's HTTPS release
host, checks its archive SHA-256 against the release manifest, and refuses to
overwrite an existing binary. It does not install globally or use sudo. The
manifest is fetched over HTTPS; this is not detached-signature verification.

With the expected Azure CLI account signed in, run:

```bash
az account set --subscription 2c209a53-b9e9-4c5e-8ecf-217c5889ecd7 &&
python3 scripts/init_backend.py
```

The initializer checks the pinned version and target subscription/tenant,
generates temporary noncredential backend settings, and runs only:
`terraform init -reconfigure -input=false -lockfile=readonly`.
It uses Entra data-plane authentication through the human CLI session, disables
OIDC for that operation, removes inherited ARM authentication/CLI-argument
overrides from its child process, and deletes its temporary settings afterward.
Local initialization metadata/provider caches stay under ignored `.terraform/`.
It never migrates state, applies infrastructure, or destroys resources.
Initialization can initialize backend metadata/an empty state blob; this is not
an AKS/ACR deployment.

Expected output includes `Successfully configured the backend "azurerm"` and
`Backend initialization succeeded using the current Azure CLI identity.`
This verifies that identity's initialization operations only, not full
read/write/locking behavior or pipeline access.

### If backend initialization reports a data-plane 403

Subscription Owner does not automatically grant Entra blob-data access. The
bootstrap granted that role to the pipeline service principal, not to your human
Cloud Shell account. If needed, an authorized administrator can explicitly grant
your user the same role at container scope:

```bash
CURRENT_USER_OBJECT_ID=$(az ad signed-in-user show --query id --output tsv) &&
az role assignment create \
  --subscription 2c209a53-b9e9-4c5e-8ecf-217c5889ecd7 \
  --assignee-object-id "$CURRENT_USER_OBJECT_ID" \
  --assignee-principal-type User \
  --role "Storage Blob Data Contributor" \
  --scope "/subscriptions/2c209a53-b9e9-4c5e-8ecf-217c5889ecd7/resourceGroups/aks-tfstate-rg/providers/Microsoft.Storage/storageAccounts/akstfstate2c209a53/blobServices/default/containers/tfstate" \
  --query '{principalId:principalId,scope:scope}' --output json
```

That command changes permissions; it is not run automatically by the initializer.
Complete MFA if required, allow role propagation, then rerun initialization.
Do not enable shared keys or fetch account keys to bypass a 403.

### Validation and optional real plan

After successful initialization, validate before planning:

```bash
terraform -chdir=terraform fmt -check -recursive &&
terraform -chdir=terraform validate
```

When authorized to inspect the proposed sandbox, a real plan (no apply) is:

```bash
terraform -chdir=terraform plan -input=false -lock-timeout=5m -out=sandbox.tfplan
```

For an empty sandbox state, expect four managed resource creations and no changes
to state-storage or existing `fitxmatt` resources. Stop if the plan differs
unexpectedly. Never use `-target` for the real lifecycle plan. Saved plans and
state may contain sensitive data; they are ignored and must not be shared.
Do not run a manual Cloud Shell `apply`; use the reviewed pipeline in step 4.

Offline local validation uses:

```bash
terraform -chdir=terraform init -backend=false -input=false
terraform -chdir=terraform validate
terraform -chdir=terraform test
python3 -m unittest discover -s tests -v
```

The five Terraform tests use a mocked provider and intentionally target resources
to avoid AzureRM's generated kubelet-identity block, which generic mocks do not
reproduce. They check sandbox naming, VMSS settings, ACR/local-account security,
node count, and group isolation. They do **not** validate the full RBAC graph or
contact Azure. Provider-schema validation covers all declared resource arguments.
The real Azure DevOps plan covers the complete graph, unlike these targeted
mocks; its subsequent successful run is recorded in step 4. Planning does not
prove resource-creation permissions or Azure capacity. Test teardown refers
only to mock state, not live resources.

Local results: Terraform formatting and schema validation passed; all five
targeted Terraform tests and all 37 Python unit tests passed. Provider packages
were downloaded from HashiCorp and their signed registry checksums locked.
Local initialization used `-backend=false` only. At that configuration commit,
live initialization and plan were still pending. The later Azure DevOps success
is recorded in step 4; no Cloud Shell init, apply, or destroy success is claimed.

## Infrastructure lifecycle: step 3 — Azure DevOps Terraform tooling

The recommended path is now Azure DevOps rather than a human Cloud Shell login.
The Cloud Shell initializer remains an optional diagnostic. Pipeline YAML is
described in step 4; this step supplies its shared runner and safety checks.

### Pipeline identity and authentication

The runner must execute inside `AzureCLI@2` using `azure-service-connection`,
with workload identity federation and `addSpnToEnvironment: true`. It checks the
task's subscription, tenant, and application ID against its authenticated CLI
context, then selects native OIDC for both Terraform and its backend.
It uses the Azure DevOps OIDC request endpoint, job OAuth token, and service
connection ID so Terraform can request fresh assertions during longer operations.
The task's initial static `idToken` is not used for Terraform authentication.

The job OAuth token is mapped only through environment variables; it is never
written into backend configuration, Git, logs, or artifacts. Service connection
client secrets, inherited ARM credentials, Terraform variable/CLI overrides,
debug logging, and nondefault workspaces are not used. The configured versions
support `ARM_OIDC_AZURE_SERVICE_CONNECTION_ID` and native token refresh.

An administrator must authorize this pipeline for the Azure Resource Manager
connection and grant the pipeline identity:

- `Storage Blob Data Contributor` on the existing `tfstate` container (the
  successful bootstrap granted this to the chosen service principal).
- Resource-management permissions for the new sandbox. Since Terraform creates
  its resource group, subscription-level Contributor is a straightforward
  **playground** setup, but broader than a production least-privilege design.
- Permission to assign/remove `AcrPull`, for example a suitably scoped/conditioned
  Role Based Access Control Administrator assignment. Contributor alone cannot
  create that assignment. Do not grant Owner simply to resolve an error.
- Read permission on provider registrations; required resource providers must
  already be registered. The runner checks them but never registers them.

These roles must be granted to the **Enterprise application**, not the app
registration. Azure DevOps environment permissions and job OAuth permissions
are separate from Azure RBAC.

### Azure DevOps environment prerequisites

Create **Pipelines → Environments → `aks-sandbox`** before the first pipeline run.
Add enabled **Approval** and **Exclusive lock** checks under **Approvals and
checks**. Authorize the intended pipeline to use the environment. The job OAuth
token needs project-metadata read access, but no check-configuration introspection
permission is required.

Azure DevOps natively evaluates the environment's configured approvals and checks
before its consuming stage starts. These controls belong to the resource owner,
not the agent script. Keep both checks enabled and review their status on the
run's stage **Checks** panel. See Microsoft's
[approvals and checks documentation](https://learn.microsoft.com/en-us/azure/devops/pipelines/process/approvals?view=azure-devops).

The agent preflight only validates the expected sandbox environment name,
positive environment ID, matching project ID, and private project visibility.
It does **not** audit check presence or claim that a checks API response proves
enforcement. Owners must retain the native checks; if an owner removes them,
the script will not detect that configuration change. Post-plan ManualValidation,
explicit confirmation, saved-plan guards, and backend locking still remain.
The script never creates, disables, or bypasses native checks.

Checks are configured outside YAML; YAML alone cannot create an approval or lock.
The create pipeline uses `lockBehavior: sequential` and one stage
to keep the environment lock through plan, review, and apply. Other lifecycle
pipelines must use that same environment; manual Terraform commands do not
participate in this whole-run lock. Backend blob locking is an additional layer.

For this one-user playground, configure your own account as an environment
approver and allow self-approval if required. A production deployment should use
independent approvers and protected branches.

### Guarded plan and saved-plan handoff

`pipeline_terraform.py` has only `plan` and `apply` operations, not destroy.
It verifies Terraform's pinned version, initializes the existing remote backend,
checks formatting/schema, then plans. Before a plan is accepted, guards require:

- Exactly the four sandbox resource addresses and the committed input values.
- Expected subscription/resource groups, region, resource names, and ACR scope.
- No imports, moved resources, deletion, or replacement. Sandbox-only creates,
  updates, and no-ops are allowed; replacement requires a separate reviewed
  approach rather than an automatic create-pipeline deletion.
- For a not-yet-created ACR, an unknown role scope must directly reference the
  sandbox registry, not another resource.

Provisioning also requires an explicit request and an exact `aks-sandbox`
confirmation. Apply verifies the binary plan's hash and a manifest binding it to
the same source/configuration hash, commit, pipeline run, Terraform version,
application, and service connection. It validates the saved plan again and
applies that plan without generating another one. Terraform itself rejects a
stale plan if the remote state changes meanwhile.

Only the binary plan and noncredential manifest are eligible for artifact
handoff. The runner captures `terraform show -json` in memory and never prints
or publishes that JSON or state. Binary plans still potentially contain
sensitive resource values: restrict run/artifact access to trusted users and set
short retention before provisioning. Source/artifact hashes detect accidental
mixups; they are not a security boundary against someone allowed to modify the
pipeline and its credentials.

Local validation for this step uses synthetic Azure DevOps responses, synthetic
Terraform plans, and mocked subprocesses, with no Azure requests. Tests cover
private-project/context checks, OIDC selection, version and provider preflight,
scope/delete guards, artifact tampering, and saved-plan-only apply. At that
tooling commit, actual Azure DevOps compilation and service-connection access
were unverified; the later plan-only execution is recorded in step 4.
All 65 Python unit tests and whitespace checks passed for this tooling commit.
This is the historical tooling-commit count; the revised context-preflight tests
and current full-suite count are recorded in step 4.

## Infrastructure lifecycle: step 4 — manual provisioning pipeline

### One-time Azure DevOps setup

1. Configure the `aks-sandbox` Azure DevOps environment, enabled Approval and
   Exclusive lock checks, and project read access described in step 3.
   These are Azure DevOps controls, not Azure resources.
2. Confirm that the workload-federated `azure-service-connection` has the Azure
   data-plane, provisioning, and role-assignment permissions listed in step 3.
   The subscription checker succeeding does **not** prove these permissions.
3. Use a **private Azure DevOps project**; the preflight rejects a public project
   before Terraform runs. The source GitHub repository can remain public.
   Limit pipeline edit/queue/run/artifact permissions to trusted users. Configure
   short pipeline-run retention (the shortest permitted by your organization)
   before using the provisioning option. Binary plans are sensitive and artifacts
   follow run retention; the YAML does not override organization retention rules.
4. Select **Pipelines → New pipeline → GitHub → `kovokilla/aks` → Existing Azure
   Pipelines YAML file**. Choose branch `feat/terraform-lifecycle` until merged,
   then `main`, and select `/azure-pipelines-create.yml`.
5. Save the pipeline. Authorize this specific pipeline for both the environment
   and service connection when prompted. No variable group or marketplace
   Terraform extension is required.

### First run: version check, backend access, and plan only

Run with the default **`provisionResources = false`**. No apply job is generated.
Approve the environment's stage-level check when prompted. The plan job then:

1. Installs checksum-verified Terraform **1.13.3** on the hosted Linux agent and
   displays its version. It does not rely on the agent's preinstalled version.
2. Verifies the private project ID/visibility and sandbox deployment context.
   Azure DevOps has already evaluated the owner-configured native checks before
   the stage starts; the agent does not query the checks configuration API.
3. Signs in through `AzureCLI@2`, verifies the expected subscription, tenant,
   application, Terraform version, and registered Azure providers.
4. Uses native refreshable workload federation to initialize the **existing**
   Azure backend at `sandbox/terraform.tfstate`.
5. Runs formatting checks, provider-schema validation, and the full real
   Terraform plan, then validates its sandbox-only changes.

This first run verifies backend initialization using the **pipeline identity**
instead of your Cloud Shell user. The successful operator-supplied run logs are
recorded below. The plan acquires/releases backend state locking as part of
Terraform's operation, but the pipeline does not conduct a separate contention
test. No application/AKS deployment is performed in plan-only mode.
No plan artifact is published in this mode; inspect the protected run's plan log.

### Provision on demand

Once the plan-only run succeeds and you are ready for Azure charges:

1. Start a **new run**, set **`provisionResources = true`**, and type
   **`aks-sandbox`** in **`confirmEnvironment`**.
2. Approve the environment stage check. A new plan is generated for this run and
   published as the restricted `sandbox-plan` artifact.
3. The agentless **ReviewPlan** job pauses. Review that run's plan log, expected
   subscription, all four sandbox addresses, sizing, and cost. The manual review
   times out and rejects after 30 minutes. With no email recipients configured,
   watch the Azure DevOps run page for the pending review.
4. **Resume** to provision or **Reject** to stop. For this playground, queue-build
   permission holders may review and self-approval is explicitly allowed.
   Use explicit independent approvers and branch protection for production.
5. After approval, the apply deployment job installs the same Terraform version,
   rechecks private-project/sandbox context, downloads the artifact from this run only,
   initializes the same backend, validates artifact/source/run/identity binding
   and plan scope, then applies the exact reviewed binary plan.

One stage references the same environment for both deployment jobs and uses
`lockBehavior: sequential`; the configured Exclusive lock check holds across
planning, agentless review, and apply. Do not split this into separate stages
without redesigning locking and stale-plan handling. The eventual destruction
pipeline must reference the same environment and state key.

There are no automatic push or PR triggers. An explicit confirmation cannot be
injected into a shell command: it is passed through an environment variable and
validated before provisioning. Deletes/replacements are rejected even when
provisioning is requested. A stale or mismatched artifact fails rather than being
silently replanned or applied.

If apply fails or is cancelled, resources can be partially created and billed.
Terraform persists state as operations complete; inspect the state and a new
full plan before retrying. Do not delete the backend, force-unlock an active run,
or rerun only the old apply job. Start a new pipeline run after resolving the
failure. ACR images, workloads, and data are not restored by recreating resources.

### Costs, capacity, and troubleshooting

AKS's Free control-plane tier does **not** make the environment free: node VMs,
managed disks, ACR, outbound networking, public IPs, and storage incur charges.
Use Azure pricing/cost management for your subscription; no live price estimate
or quota check has been performed. The apply job requests 120 minutes, but an
organization's free hosted-job time limit may be lower and still terminate it.
Have sufficient hosted parallel-job capacity and time allowance before applying.

- **Missing environment/permission prompt:** create/authorize the correct
  environment and connection; do not select the checker YAML by mistake.
- **Project-metadata API 401/403:** check project read access and job OAuth mapping.
  This is separate from Azure IAM. Do not grant Administrator or a PAT merely
  to inspect environment check configuration.
- **Old "Observed API check types: none" error:** use the latest branch and start
  a new run. That custom checks-API probe was removed after repeated empty
  responses despite the UI showing both checks and explicit Reader access.
  The cause of those empty responses was not conclusively established. This is
  not an empty-response-as-success fallback: check introspection is no longer
  performed, and actual enforcement remains Azure DevOps's responsibility.
- **No native approval/lock visible on the stage:** stop before provisioning and
  verify owner-configured enabled checks on the `aks-sandbox` environment and
  both deployment jobs' environment references. Do not disable the native checks.
  `Lock not in use` on an idle environment is normal, not a disabled lock.
- **OIDC missing/invalid:** the connection must use workload federation, not a
  GitHub connection or client-secret connection. Required OAuth/request URI
  variables and the task-provided service connection ID must be available.
- **Backend 403:** verify the actual service principal has container-scoped
  Storage Blob Data Contributor and wait for propagation. Do not enable keys.
- **Provider not registered:** an administrator must register the named provider
  separately. The pipeline deliberately does not do this during plan.
- **Resource/RBAC authorization failure:** grant appropriately scoped deployment
  and `AcrPull` assignment rights; don't confuse application and principal IDs.
- **Name/SKU/quota/version/policy failure:** adjust committed sandbox inputs
  after reviewing the Azure error, then queue a new full plan.
- **Replacement/delete guard failure:** stop and review why replacement is
  needed. This create pipeline is not an implicit destroy mechanism.

### Validation record

Local validation covers both YAML files' syntax and plan-only/provisioning job
structure, Bash syntax, all 66 offline Python tests, and the existing Terraform
format/schema checks and five targeted mock tests. The unchanged subscription
checker is preserved. The first operator-reported Azure DevOps run reached the
environment preflight and stopped there; a subsequent screenshot showed both
checks configured. Label compatibility and safe diagnostics were improved, but
updated runs then reported empty responses from both documented APIs even after
Reader access was made explicit. The redundant probe was removed; private-project
and sandbox-context checks, native environment references, sequential lock mode,
post-plan manual approval, and saved-plan safeguards are preserved.
Obsolete API-parser tests were replaced with context and native-gate wiring tests.

### Live plan-only validation — 2026-10-18

After the revised preflight, the operator supplied this successful Azure DevOps
Terraform task output:

```text
Plan: 4 to add, 0 to change, 0 to destroy.
Releasing state lock. This may take a few moments...
Terraform version check passed: 1.13.3
Sandbox-only plan validated; review the Terraform plan log before approving.
Finishing: Terraform plan with federated Azure authentication
```

The planned outputs matched `aks-sandbox`, `aks-sandbox-nodes-rg`, and
`aks-sandbox-rg`; the ACR login server remains known only after apply.
This verifies the plan-only pipeline path: the pinned version check, native
federated authentication, existing Azure backend initialization/access, schema
validation, full four-resource plan, sandbox guards, and normal state-lock
acquisition/release completed. This is operator-provided log evidence, not an
independently queried Azure inventory or run API result. No run URL was supplied.

**Nothing was provisioned by this plan-only run.** Four additions are proposed,
not existing deployed resources. No changes/deletions were planned, and no
apply/destroy was executed as part of this validation.
Successful planning does not prove apply-time provisioning/role-assignment
permissions, SKU/quota/name availability, lease contention handling, token refresh
over a long deployment, or state recovery. The provisioning artifact/review/apply
path and separate destruction pipeline remain unverified.

When explicitly ready to incur sandbox charges, start a new run with
`provisionResources = true`, confirm `aks-sandbox`, and review that new run's plan
before approving it. The previously completed plan-only run is not an apply
authorization and is not automatically reused.
