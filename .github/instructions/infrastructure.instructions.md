---
description: Azure deployment, identity, networking, and secret-handling invariants
applyTo: "infra/**,deployments/**,hooks/**,azure.yaml,deploy.sh,Dockerfile,**/Dockerfile*,.github/workflows/**"
---

# Infrastructure conventions

- Use Bicep and suitable Azure Verified Modules for shippable Azure resources;
  native modules cover gaps. Keep `azd` and the root `azure.yaml` as the supported
  orchestration boundary for `web-nat` and `card-orchestrator`.
- Follow current [deployment guidance](../../README.md#azure-deployment) and
  [infra documentation](../../infra/README.md), not historical commands.
  Inspect lifecycle hooks before provisioning. Web-only code releases use
  targeted `azd deploy web-nat`; never assume a bare deploy is web-only.
- Preserve the deployed web image during repeated provisioning. Use the public
  placeholder only for an empty first bootstrap, never in place of a live image.
- Treat hosted-agent artifact/version identifiers as immutable. Keep azd state,
  endpoint routing, web configuration, and readiness aligned to the exact active
  version. Roll back explicitly using the approved artifact and supported
  lifecycle hooks, never by silently enabling a direct-model fallback.
- Cosmos DB, Blob Storage, and Key Vault remain private-endpoint-only with
  public network access disabled. NAT provides public-service egress, not the
  Cosmos connectivity path. Do not reintroduce service-endpoint/firewall/SAS
  workarounds for private data access.
- Use managed identities and least-privilege RBAC. Resolve `deployer().objectId`
  once at the root and pass it to modules; retain explicit principal types for
  ARM assignments. Runtime Key Vault access uses the Container App identity;
  Blob grants are container-scoped where possible.
- Secret-producing hooks must never enable `set -x`, print secret values,
  implicitly revoke working credentials, or rotate existing secrets during
  routine reprovisioning. Preserve existing session/Entra credentials and
  bounded overlap during explicit rotations.
- ARM `mul()`/`div()` reject Float operands regardless of operand order. Move
  fractional arithmetic into KQL/downstream consumers or use scaled integers.
  Bicep builds are insufficient: ARM expression changes also require an
  authorized deployment preview.
- Check live regional catalog/quota support before model changes; parameterize
  names, versions, SKUs, and capacities. Do not treat static validation as proof
  of live permissions, availability, safety, or deployment success.
- Keep developer state, credentials, tests, and unrelated source out of image
  build contexts; preserve allowlists and defense-in-depth exclusions.
