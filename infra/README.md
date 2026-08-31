# Infrastructure deployment notes

## Monitoring configuration

Each `dev`/`prod` deployment owns an isolated Log Analytics workspace,
workspace-based Application Insights component, workbook, availability test, Action
Group, and eight alert rules. The default workspace retention is 30 days, its daily
cap is 0.25 GB, and parent-consistent trace sampling is 100%.

The initial rollout is dashboard-only: receiver arrays are empty, the Action Group is
disabled, and `MONITORING_ALERTS_ENABLED=false`, so all eight rules deploy disabled.
Adding an approved receiver enables the Action Group. After the dashboard has been
calibrated for traffic below 100 requests/day, an operator can explicitly enable the
rules.

```powershell
azd env set MONITORING_RETENTION_DAYS 30
azd env set MONITORING_DAILY_QUOTA_GB 0.25
azd env set MONITORING_INGESTION_WARNING_PERCENT 80
azd env set TELEMETRY_SAMPLING_RATIO 1.0
azd env set MONITORING_ALERTS_ENABLED false
azd env set MONITORING_REQUEST_TRAFFIC_FLOOR 5
azd env set MONITORING_EMAIL_RECEIVERS '[]'
azd env set MONITORING_WEBHOOK_RECEIVERS '[]'
```

See [`../docs/operational-monitoring.md`](../docs/operational-monitoring.md) for
receiver object schemas, thresholds, supported workspace table names, KQL, privacy
rules, cost estimation, deployment verification, and rollback. No monitoring
resources are deployed by repository changes alone; deployment remains a separate,
explicitly authorized `azd` operation.

## Dev/MVP NAT Gateway baseline for Cosmos egress

Issue [#35](https://github.com/bmoussaud/fantasy-cards-generator/issues/35)
adds the current dev/MVP network baseline:

- a dedicated VNet
- a delegated `aca-infra` subnet for a workload-profile Container Apps environment
- a NAT Gateway with a static public IP on that subnet
- Cosmos DB `ipRules` sourced from the NAT public IP

This keeps the app on the normal Cosmos public FQDN with managed-identity
data-plane auth. It does **not** introduce a private endpoint, connection
string, or Cosmos keys.

> This is **dev/MVP only**. Production hardening belongs to
> [#37](https://github.com/bmoussaud/fantasy-cards-generator/issues/37), which
> upgrades the Cosmos trust model to subnet-scoped service-endpoint rules.

## Provisioning

Use the normal azd workflow:

```bash
azd env new dev
azd env set AZURE_LOCATION eastus2
azd env set LEGACY_COSMOS_IP_RULE 20.10.253.231
azd up
```

`azd` supplies `AZURE_PRINCIPAL_ID` and `AZURE_PRINCIPAL_TYPE` from the
currently signed-in Microsoft Entra principal. Provisioning passes those
values to Bicep and assigns that principal the built-in **Foundry User** role
(formerly **Azure AI User**) at the deployed Foundry project scope. The
principal object ID is configuration, not a credential; do not copy it into
source or replace it with a hard-coded value.

The project-scoped assignment is intentionally separate from the existing
account-scoped **Cognitive Services User** assignment used by the Container
App managed identity. A direct Bicep deployment that bypasses `azd` must pass
the deployer's Entra object ID and principal type explicitly as
`deployerPrincipalId` and `deployerPrincipalType`.

Why the extra env var:

- `natGatewayPublicIpAddress` is provisioned by IaC and always lands in Cosmos
  `ipRules`.
- `LEGACY_COSMOS_IP_RULE` is only a temporary cutover aid so the pre-NAT ACA
  incident allowlist can stay in place until the new path is proven in a real
  deployment.
- Clear it after validation:

```bash
azd env set LEGACY_COSMOS_IP_RULE ""
azd provision
```

## Parallel cutover

ACA VNet mode is effectively a create-time choice, so this repo now targets a
replacement Container Apps environment/app pair (`*-cae-nat`, `*-app-nat`)
instead of mutating the original environment in place.

Recommended rollout:

1. Leave the old Container Apps environment and the live
   `20.10.253.231` Cosmos rule available.
2. Run `azd up` so the NAT-backed environment, subnet, public IP, and Cosmos
   NAT rule are provisioned in parallel.
3. Smoke-test the new app endpoint and authenticated generation flow.
4. Only after successful validation, clear `LEGACY_COSMOS_IP_RULE` and rerun
   `azd provision` to remove the temporary incident rule from the desired state.

## Smoke-test / operational checks

After a live deploy, verify:

1. `azd env get-values` reports `AZURE_CONTAINER_APP_NAME`,
   `AZURE_CONTAINER_APPS_ENVIRONMENT_NAME`, and `natGatewayPublicIpAddress`
   outputs for the replacement environment.
2. The Cosmos account still shows `publicNetworkAccess = Enabled`.
3. Cosmos `ipRules` contains the NAT public IP and, during cutover, the legacy
   stopgap IP.
4. The deployed app still uses the normal Cosmos endpoint
   (`https://<account>.documents.azure.com:443/`) and managed identity.
5. An authenticated `POST /ui/cards/generate` (or equivalent end-to-end flow)
   persists successfully to Cosmos from the NAT-backed environment.
6. Once verified, remove the temporary legacy rule by clearing
   `LEGACY_COSMOS_IP_RULE`.
7. In the Foundry project's **Access control (IAM)**, verify the signed-in
   deployer has **Foundry User** at the project scope, then open the project
   using Microsoft Entra authentication.

The key live-network assertion — ACA outbound traffic actually using the NAT
public IP — requires an Azure deployment and cannot be proven from source alone.

## Rollback

If the replacement environment fails validation:

1. Keep the old Container Apps environment running.
2. Do not clear `LEGACY_COSMOS_IP_RULE`.
3. Point traffic/deployment back to the old environment if you temporarily
   switched consumers to the replacement app URL.
4. Fix the NAT-backed path in IaC, then redeploy and re-test.

Because the old environment is preserved in parallel, rollback is primarily a
matter of continuing to use the pre-cutover app while the new environment is
reworked.

## NAT public IP replacement implications

If the NAT Gateway public IP resource is ever replaced, Azure will allocate a
different static address unless the same Public IP resource is preserved.
Because Cosmos `ipRules` are wired from the NAT Gateway output, a subsequent
`azd provision` will update the desired firewall rule automatically — but you
must still smoke-test the app again before removing any fallback rules.

## Entra redirect verification

The replacement Container Apps environment gets a different default domain, so
OIDC redirect URIs must be rechecked after the first live deploy.

- If `deployEntraAppRegistration=true`, `infra/main.bicep` derives the deployed
  redirect URI from the new Container Apps environment domain automatically.
- If the app registration is still managed manually, update the registered web
  redirect URI to the replacement app URL plus `/auth/callback`.

Either way, verify authenticated sign-in against the live replacement domain
after deployment. Do not guess or pre-change portal values without the actual
deployed hostname.
