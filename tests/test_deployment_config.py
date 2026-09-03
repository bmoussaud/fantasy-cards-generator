import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _bicep_block(source: str, declaration: str) -> str:
    declaration_start = source.index(declaration)
    block_start = source.index("{", declaration_start)
    depth = 0

    for index in range(block_start, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[declaration_start : index + 1]

    raise AssertionError(f"Unclosed Bicep block: {declaration}")


def test_dockerfile_serves_fastapi_on_port_8000() -> None:
    dockerfile = (REPO_ROOT / "Dockerfile").read_text()

    assert "FROM python:3.12-slim" in dockerfile
    assert "USER appuser" in dockerfile
    assert "EXPOSE 8000" in dockerfile
    assert '"uvicorn", "app.entrypoint:app"' in dockerfile
    assert '"--host", "0.0.0.0"' in dockerfile
    assert '"--port", "8000"' in dockerfile


def test_dockerfile_packages_static_design_system_assets() -> None:
    dockerfile = (REPO_ROOT / "Dockerfile").read_text()

    # The Dockerfile copies the whole `app` package as a single layer, so the
    # mounted static asset tree (app/static/**) ships with the container
    # without needing a dedicated COPY line.
    assert "COPY app ./app" in dockerfile

    static_dir = REPO_ROOT / "app" / "static"
    assert (static_dir / "css" / "app.css").is_file()
    assert (static_dir / "js" / "app.js").is_file()


def test_azd_service_wires_container_app_and_acr() -> None:
    azure_yaml = (REPO_ROOT / "azure.yaml").read_text()

    assert "services:" in azure_yaml
    assert "web-nat:" in azure_yaml
    assert "host: containerapp" in azure_yaml
    assert "registry: ${AZURE_CONTAINER_REGISTRY_ENDPOINT}" in azure_yaml
    assert "resources:" in azure_yaml
    assert "port: 8000" in azure_yaml


def test_bicep_exposes_azd_container_outputs_without_helloworld_image() -> None:
    main_bicep = (REPO_ROOT / "infra" / "main.bicep").read_text()
    container_apps_bicep = (REPO_ROOT / "infra" / "modules" / "container-apps.bicep").read_text()

    # The helloworld image is allowed in main.bicep only as a safe-provision
    # conditional fallback (when containerImage is empty on first provision).
    # It must never be hard-coded unconditionally, and must not leak into the
    # container-apps module (which always receives the resolved image value).
    helloworld_image = "mcr.microsoft.com/azuredocs/containerapps-helloworld"
    assert helloworld_image not in container_apps_bicep
    # When present in main.bicep it must be guarded by an empty() conditional.
    if helloworld_image in main_bicep:
        assert f"empty(containerImage) ? '{helloworld_image}" in main_bicep, (
            "helloworld image must only appear as the conditional fallback "
            "when containerImage is empty"
        )
    assert "modules/container-registry.bicep" in main_bicep
    assert "output AZURE_CONTAINER_REGISTRY_ENDPOINT" in main_bicep
    assert "output AZURE_CONTAINER_APP_NAME" in main_bicep
    assert "param serviceName string = 'web-nat'" in main_bicep
    assert "param serviceName string = 'web-nat'" in container_apps_bicep
    assert "targetPort: 8000" in container_apps_bicep
    assert "registries:" in container_apps_bicep


def test_generation_runtime_env_vars_are_wired_from_bicep_outputs() -> None:
    container_apps_bicep = (REPO_ROOT / "infra" / "modules" / "container-apps.bicep").read_text()
    main_bicep = (REPO_ROOT / "infra" / "main.bicep").read_text()

    assert "name: 'AI_MODE'" in container_apps_bicep
    assert "name: 'PERSISTENCE_MODE'" in container_apps_bicep
    assert "name: 'FOUNDRY_ENDPOINT'" in container_apps_bicep
    assert "name: 'FOUNDRY_TEXT_DEPLOYMENT'" in container_apps_bicep
    assert "name: 'FOUNDRY_IMAGE_DEPLOYMENT'" in container_apps_bicep
    assert "name: 'COSMOS_ENDPOINT'" in container_apps_bicep
    assert "name: 'COSMOS_DATABASE_NAME'" in container_apps_bicep
    assert "name: 'COSMOS_CONTAINER_NAME'" in container_apps_bicep
    assert "name: 'BLOB_ENDPOINT'" in container_apps_bicep
    assert "name: 'BLOB_CONTAINER_NAME'" in container_apps_bicep
    assert "name: 'MODERATION_SERVICE'" in container_apps_bicep
    assert "name: 'MODERATION_POLICY_NAME'" in container_apps_bicep
    assert "name: 'RATE_LIMIT_USER_REQUESTS'" in container_apps_bicep
    assert "name: 'RATE_LIMIT_IP_REQUESTS'" in container_apps_bicep
    assert "name: 'TRUSTED_PROXY_HOPS'" in container_apps_bicep
    assert "name: 'TEXT_TIMEOUT_SECONDS'" in container_apps_bicep
    assert "name: 'IMAGE_TIMEOUT_SECONDS'" in container_apps_bicep
    assert "name: 'IMAGE_MAX_RETRIES'" in container_apps_bicep
    assert "name: 'OVERALL_TIMEOUT_SECONDS'" in container_apps_bicep
    assert "name: 'AUDIT_RETENTION_DAYS'" in container_apps_bicep
    assert "name: 'IMAGE_SIZE'" in container_apps_bicep
    assert "name: 'IMAGE_QUALITY'" in container_apps_bicep

    assert (
        "foundryEndpoint: 'https://${aiFoundryAccountName}.cognitiveservices.azure.com/'"
        in main_bicep
    )
    assert "foundryTextDeployment: aiFoundryTextDeploymentName" in main_bicep
    assert "foundryImageDeployment: aiFoundryImageDeploymentName" in main_bicep
    assert "cosmosEndpoint: 'https://${cosmosAccountName}.documents.azure.com:443/'" in main_bicep
    assert (
        "blobEndpoint: 'https://${storageAccountName}.blob.${environment().suffixes.storage}/'"
        in main_bicep
    )
    assert "trustedProxyHops: trustedProxyHops" in main_bicep


def test_deployer_gets_foundry_user_at_project_scope() -> None:
    main_bicep = (REPO_ROOT / "infra" / "main.bicep").read_text()
    main_parameters = json.loads((REPO_ROOT / "infra" / "main.parameters.json").read_text())
    foundry_bicep = (REPO_ROOT / "infra" / "modules" / "ai-foundry.bicep").read_text()
    foundry_module = _bicep_block(main_bicep, "module aiFoundry './modules/ai-foundry.bicep'")
    deployer_assignment = _bicep_block(foundry_bicep, "resource deployerFoundryUserRoleAssignment")
    runtime_assignment = _bicep_block(foundry_bicep, "resource cognitiveServicesUserRoleAssignment")

    assert main_bicep.count("deployer().objectId") == 1
    assert "param deployerPrincipalId" not in main_bicep
    assert "deployerPrincipalId" not in main_parameters["parameters"]
    assert (
        main_parameters["parameters"]["deployerPrincipalType"]["value"] == "${AZURE_PRINCIPAL_TYPE}"
    )
    assert "deployerPrincipalId: deployerPrincipalId" in foundry_module
    assert "deployerPrincipalType: deployerPrincipalType" in foundry_module

    assert "var foundryUserRoleDefinitionId = '53ca6127-db72-4b80-b1b0-d745d6d5456d'" in (
        foundry_bicep
    )
    assert (
        "resource deployerFoundryUserRoleAssignment "
        "'Microsoft.Authorization/roleAssignments@2022-04-01'" in foundry_bicep
    )
    assert "scope: aiFoundryProject" in deployer_assignment
    assert "principalId: deployerPrincipalId" in deployer_assignment
    assert "principalType: deployerPrincipalType" in deployer_assignment
    assert (
        "guid(aiFoundryProject.id, deployerPrincipalId, foundryUserRoleDefinitionId)"
        in deployer_assignment
    )
    assert (
        "roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', "
        "foundryUserRoleDefinitionId)" in deployer_assignment
    )

    # Runtime access remains a separate assignment for the Container App identity.
    assert "scope: foundryAccount" in runtime_assignment
    assert "principalId: containerAppPrincipalId" in runtime_assignment
    assert "cognitiveServicesUserRoleDefinitionId" in runtime_assignment


def test_deployer_gets_cosmos_data_reader_at_account_root() -> None:
    main_bicep = (REPO_ROOT / "infra" / "main.bicep").read_text()
    cosmos_bicep = (REPO_ROOT / "infra" / "modules" / "cosmos-db.bicep").read_text()
    cosmos_module = _bicep_block(main_bicep, "module cosmosDb './modules/cosmos-db.bicep'")
    deployer_assignment = _bicep_block(
        cosmos_bicep, "resource deployerCosmosDataReaderRoleAssignment"
    )
    runtime_assignment = _bicep_block(cosmos_bicep, "resource sqlRoleAssignment")

    assert "deployerPrincipalId: deployerPrincipalId" in cosmos_module
    assert (
        "var cosmosDataReaderRoleDefinitionId = '00000000-0000-0000-0000-000000000001'"
        in cosmos_bicep
    )
    assert (
        "'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-05-15'"
        in deployer_assignment
    )
    assert "principalId: deployerPrincipalId" in deployer_assignment
    assert "scope: databaseAccount.id" in deployer_assignment
    assert "principalType:" not in deployer_assignment
    assert (
        "guid(databaseAccount.id, deployerPrincipalId, cosmosDataReaderRoleDefinitionId)"
        in deployer_assignment
    )
    assert (
        "'${databaseAccount.id}/sqlRoleDefinitions/${cosmosDataReaderRoleDefinitionId}'"
        in deployer_assignment
    )

    assert "principalId: containerAppPrincipalId" in runtime_assignment
    assert "cosmosDataContributorRoleDefinitionId" in runtime_assignment
    assert "scope: '${databaseAccount.id}/dbs/${databaseName}/colls/${containerName}'" in (
        runtime_assignment
    )


def test_deployer_blob_reader_at_account_scope_runtime_container_scoped() -> None:
    main_bicep = (REPO_ROOT / "infra" / "main.bicep").read_text()
    storage_bicep = (REPO_ROOT / "infra" / "modules" / "storage.bicep").read_text()
    storage_module = _bicep_block(main_bicep, "module storage './modules/storage.bicep'")
    deployer_assignment = _bicep_block(
        storage_bicep, "resource deployerStorageBlobDataReaderRoleAssignment"
    )
    runtime_assignment = _bicep_block(
        storage_bicep, "resource storageBlobDataContributorRoleAssignment"
    )
    delegator_assignment = _bicep_block(
        storage_bicep, "resource storageBlobDelegatorRoleAssignment"
    )

    assert "deployerPrincipalId: deployerPrincipalId" in storage_module
    assert "deployerPrincipalType: deployerPrincipalType" in storage_module
    assert (
        "var storageBlobDataReaderRoleDefinitionId = "
        "'2a2b9908-6ea1-4ae2-8e65-a410df84e7d1'" in storage_bicep
    )
    assert (
        "var storageBlobDelegatorRoleDefinitionId = "
        "'db58b8e5-c6ad-4a2a-8342-4190687cbf4a'" in storage_bicep
    )
    assert "'Microsoft.Authorization/roleAssignments@2022-04-01'" in deployer_assignment
    assert "scope: storageAccount" in deployer_assignment
    assert "principalId: deployerPrincipalId" in deployer_assignment
    assert "principalType: deployerPrincipalType" in deployer_assignment
    assert (
        "guid(storageAccount.id, deployerPrincipalId, storageBlobDataReaderRoleDefinitionId)"
        in deployer_assignment
    )
    assert (
        "subscriptionResourceId('Microsoft.Authorization/roleDefinitions', "
        "storageBlobDataReaderRoleDefinitionId)" in deployer_assignment
    )

    assert "principalId: containerAppPrincipalId" in runtime_assignment
    assert "principalType: 'ServicePrincipal'" in runtime_assignment
    assert "storageBlobDataContributorRoleDefinitionId" in runtime_assignment
    assert "scope: cardAssetsContainer" in runtime_assignment
    assert "scope: storageAccount" not in runtime_assignment
    assert (
        "guid(cardAssetsContainer.id, containerAppPrincipalId, "
        "storageBlobDataContributorRoleDefinitionId)" in runtime_assignment
    )

    assert "principalId: containerAppPrincipalId" in delegator_assignment
    assert "principalType: 'ServicePrincipal'" in delegator_assignment
    assert "scope: storageAccount" in delegator_assignment
    assert "storageBlobDelegatorRoleDefinitionId" in delegator_assignment
    assert (
        "guid(storageAccount.id, containerAppPrincipalId, "
        "storageBlobDelegatorRoleDefinitionId)" in delegator_assignment
    )


def test_deployer_gets_key_vault_reader_at_vault_scope() -> None:
    main_bicep = (REPO_ROOT / "infra" / "main.bicep").read_text()
    main_parameters = json.loads((REPO_ROOT / "infra" / "main.parameters.json").read_text())
    security_bicep = (REPO_ROOT / "infra" / "modules" / "security.bicep").read_text()
    security_module = _bicep_block(main_bicep, "module security './modules/security.bicep'")
    deployer_assignment = _bicep_block(
        security_bicep, "resource deployerKeyVaultReaderRoleAssignment"
    )
    runtime_assignment = _bicep_block(security_bicep, "resource keyVaultSecretsUserRoleAssignment")

    assert main_bicep.count("deployer().objectId") == 1
    assert "param deployerPrincipalId" not in main_bicep
    assert "deployerPrincipalId" not in main_parameters["parameters"]
    assert (
        main_parameters["parameters"]["deployerPrincipalType"]["value"] == "${AZURE_PRINCIPAL_TYPE}"
    )
    assert "deployerPrincipalId: deployerPrincipalId" in security_module
    assert "deployerPrincipalType: deployerPrincipalType" in security_module

    assert (
        "var keyVaultReaderRoleDefinitionId = '21090545-7ca7-4776-b22c-e363652d74d2'"
        in security_bicep
    )
    assert (
        "resource deployerKeyVaultReaderRoleAssignment "
        "'Microsoft.Authorization/roleAssignments@2022-04-01'" in security_bicep
    )
    assert "scope: keyVault" in deployer_assignment
    assert "principalId: deployerPrincipalId" in deployer_assignment
    assert "principalType: deployerPrincipalType" in deployer_assignment
    assert (
        "guid(keyVault.id, deployerPrincipalId, keyVaultReaderRoleDefinitionId)"
        in deployer_assignment
    )
    assert (
        "roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', "
        "keyVaultReaderRoleDefinitionId)" in deployer_assignment
    )

    # Runtime secret-value access remains a separate assignment for the
    # Container App identity.
    assert "scope: keyVault" in runtime_assignment
    assert "principalId: keyVaultAccessPrincipalId" in runtime_assignment
    assert "principalType: 'ServicePrincipal'" in runtime_assignment
    assert "keyVaultSecretsUserRoleDefinitionId" in runtime_assignment


def test_healthz_dependency_probe_rbac_and_timeouts_are_iac_managed() -> None:
    main_bicep = (REPO_ROOT / "infra" / "main.bicep").read_text()
    main_parameters = (REPO_ROOT / "infra" / "main.parameters.json").read_text()
    container_apps_bicep = (REPO_ROOT / "infra" / "modules" / "container-apps.bicep").read_text()
    cosmos_bicep = (REPO_ROOT / "infra" / "modules" / "cosmos-db.bicep").read_text()
    storage_bicep = (REPO_ROOT / "infra" / "modules" / "storage.bicep").read_text()
    readme = (REPO_ROOT / "infra" / "README.md").read_text()
    runtime_cosmos_assignment = _bicep_block(cosmos_bicep, "resource sqlRoleAssignment")
    runtime_blob_assignment = _bicep_block(
        storage_bicep, "resource storageBlobDataContributorRoleAssignment"
    )

    assert "scope: '${databaseAccount.id}/dbs/${databaseName}/colls/${containerName}'" in (
        runtime_cosmos_assignment
    )
    assert "scope: cardAssetsContainer" in runtime_blob_assignment
    assert (
        "param healthzCosmosTimeoutMs int = 1500" in main_bicep
        and "param healthzBlobTimeoutMs int = 1500" in main_bicep
    )
    assert (
        "param healthzCosmosTimeoutMs int = 1500" in container_apps_bicep
        and "param healthzBlobTimeoutMs int = 1500" in container_apps_bicep
    )
    assert "healthzCosmosTimeoutMs: healthzCosmosTimeoutMs" in main_bicep
    assert "healthzBlobTimeoutMs: healthzBlobTimeoutMs" in main_bicep
    assert "name: 'HEALTHZ_COSMOS_TIMEOUT_MS'" in container_apps_bicep
    assert "value: string(healthzCosmosTimeoutMs)" in container_apps_bicep
    assert "name: 'HEALTHZ_BLOB_TIMEOUT_MS'" in container_apps_bicep
    assert "value: string(healthzBlobTimeoutMs)" in container_apps_bicep
    assert '"value": "${HEALTHZ_COSMOS_TIMEOUT_MS=1500}"' in main_parameters
    assert '"value": "${HEALTHZ_BLOB_TIMEOUT_MS=1500}"' in main_parameters

    # Healthz probe tuning stays non-secret: no secure params, secret refs,
    # account keys, or connection strings were introduced for these settings.
    assert "@secure()\n@description('Bounded Cosmos metadata probe timeout" not in main_bicep
    assert "@secure()\n@description('Bounded Blob container-properties probe" not in main_bicep
    assert "secretRef: 'healthz-cosmos-timeout-ms'" not in container_apps_bicep
    assert "secretRef: 'healthz-blob-timeout-ms'" not in container_apps_bicep
    assert "HEALTHZ_COSMOS_TIMEOUT_MS_CONNECTION_STRING" not in container_apps_bicep
    assert "HEALTHZ_BLOB_TIMEOUT_MS_CONNECTION_STRING" not in container_apps_bicep
    assert "HEALTHZ_COSMOS_TIMEOUT_MS" not in (
        (REPO_ROOT / "infra" / "modules" / "security.bicep").read_text()
    )
    assert "HEALTHZ_BLOB_TIMEOUT_MS" not in (
        (REPO_ROOT / "infra" / "modules" / "security.bicep").read_text()
    )

    assert "/healthz" in readme
    assert "deployer().objectId" in readme
    assert "AZURE_PRINCIPAL_TYPE" in readme
    assert "Key Vault Reader" in readme
    assert "key vault scope" in readme.lower()
    assert "HEALTHZ_COSMOS_TIMEOUT_MS" in readme
    assert "HEALTHZ_BLOB_TIMEOUT_MS" in readme
    assert "readiness every 10s" in readme
    assert "liveness every 30s" in readme


def test_container_apps_wire_key_vault_backed_auth_env_vars() -> None:
    container_apps_bicep = (REPO_ROOT / "infra" / "modules" / "container-apps.bicep").read_text()
    security_bicep = (REPO_ROOT / "infra" / "modules" / "security.bicep").read_text()
    main_bicep = (REPO_ROOT / "infra" / "main.bicep").read_text()
    main_parameters = (REPO_ROOT / "infra" / "main.parameters.json").read_text()

    # APP_SESSION_SECRET_KEY and ENTRA_CLIENT_SECRET are mirrored into ACA-native
    # secrets, while still flowing through secure azd/Bicep inputs upstream.
    assert "name: 'APP_SESSION_SECRET_KEY'" in container_apps_bicep
    assert "value: appSessionSecretKeyValue" in container_apps_bicep
    assert "secretRef: 'app-session-secret-key'" in container_apps_bicep
    assert "name: 'ENTRA_CLIENT_SECRET'" in container_apps_bicep
    assert "value: entraClientSecretValue" in container_apps_bicep
    assert "secretRef: 'entra-client-secret'" in container_apps_bicep
    assert "keyVaultUrl:" not in container_apps_bicep
    assert "appSessionSecretKeySecretUri" not in container_apps_bicep
    assert "entraClientSecretSecretUri" not in container_apps_bicep

    # ENTRA_CLIENT_ID is a plain env var, not a secretRef.
    assert "name: 'ENTRA_CLIENT_ID'" in container_apps_bicep
    assert "value: entraClientId" in container_apps_bicep

    # Redirect URIs are plain env vars auto-injected from the deployed URL,
    # never sourced from a manually supplied param default.
    assert "name: 'ENTRA_REDIRECT_URI'" in container_apps_bicep
    assert "name: 'ENTRA_POST_LOGOUT_REDIRECT_URI'" in container_apps_bicep

    # ENTRA_AUTHORITY / ENTRA_SCOPES stay out of scope: rely on app code defaults.
    assert "ENTRA_AUTHORITY" not in container_apps_bicep
    assert "ENTRA_SCOPES" not in container_apps_bicep

    # Upstream inputs stay secure: azd passes env-backed values into secure Bicep
    # params instead of hardcoding plaintext into the template.
    assert "@secure()" in main_bicep
    assert "param appSessionSecretKeyValue string = ''" in main_bicep
    assert "param entraClientSecretValue string = ''" in main_bicep
    assert '"value": "${APP_SESSION_SECRET_KEY=}"' in main_parameters
    assert '"value": "${ENTRA_CLIENT_SECRET=}"' in main_parameters

    # Key Vault still stores the provisioned secrets.
    assert "Microsoft.KeyVault/vaults/secrets" in security_bicep
    assert "'app-session-secret-key'" in security_bicep
    assert "'entra-client-secret'" in security_bicep

    # The deployed redirect URI must be derived from the Container Apps
    # environment domain, not depend on the container app's own output
    # (which would create a circular module dependency).
    assert "deployedAuthRedirectUri" in main_bicep
    assert "containerAppsEnvironmentDefaultDomain" in main_bicep


def test_cosmos_container_enables_item_level_ttl() -> None:
    cosmos_bicep = (REPO_ROOT / "infra" / "modules" / "cosmos-db.bicep").read_text()

    assert "defaultTtl: -1" in cosmos_bicep


def test_cosmos_private_endpoint_bypasses_pna_disabled_governance_policy() -> None:
    """Assert Private Endpoint posture for Cosmos DB (approved path, 2026-09-02).

    Root cause (established 2026-09-02 by independent Gandalf investigation):
    Azure Policy 'CosmosDB_PublicNetwork_Modify' (MCAPSGovDeployPolicies initiative,
    effect: modify) is assigned at Management Group 31b6a5c6-8762-4d6b-bf6e-f37931c67a75
    and forcibly keeps publicNetworkAccess: Disabled on every Cosmos account in this
    tenant.  This is an SFI governance policy — NOT a Serverless platform limitation.
    Every REST PATCH to set publicNetworkAccess: Enabled returns HTTP 200/Succeeded but
    the value snaps back asynchronously.  No resource locks or RG/subscription-level
    policies are involved.

    Approved unblocking path (Benoit, 2026-09-02):
    Deploy a Private Endpoint for the Cosmos account into the private-endpoints subnet.
    Private Endpoint connections bypass publicNetworkAccess: Disabled; the Cosmos FQDN
    (*.documents.azure.com) resolves to a private IP via privatelink.documents.azure.com
    from within the VNet, so the Container App connects without touching the public path.

    IaC posture:
    - cosmos-private-endpoint.bicep: PE (group ID: Sql), private DNS zone
      (privatelink.documents.azure.com), VNet link, DNS zone group.
    - publicNetworkAccess: 'Disabled' retained in cosmos-db.bicep — matches governance
      policy and is correct for Private Endpoint access.
    - isVirtualNetworkFilterEnabled: false and virtualNetworkRules: [] — VNet service
      endpoint filter is inactive; the VNet path goes through the PE, not a service ep.
    - Microsoft.AzureCosmosDB service endpoint REMOVED from aca-infra subnet — no longer
      needed once the PE path is validated.
    - ipRules retained (inert under PNA: Disabled; documented, not cleaned until further
      instruction).
    """
    cosmos_bicep = (REPO_ROOT / "infra" / "modules" / "cosmos-db.bicep").read_text()
    cosmos_pe_bicep = (
        REPO_ROOT / "infra" / "modules" / "cosmos-private-endpoint.bicep"
    ).read_text()
    main_bicep = (REPO_ROOT / "infra" / "main.bicep").read_text()
    main_parameters = (REPO_ROOT / "infra" / "main.parameters.json").read_text()
    network_bicep = (REPO_ROOT / "infra" / "modules" / "network.bicep").read_text()
    container_apps_environment_bicep = (
        REPO_ROOT / "infra" / "modules" / "container-apps-environment.bicep"
    ).read_text()

    # Cosmos PE module: correct group ID, DNS zone, VNet link, DNS zone group
    assert "groupIds: [" in cosmos_pe_bicep
    assert "'Sql'" in cosmos_pe_bicep
    assert "privatelink.documents.azure.com" in cosmos_pe_bicep
    assert "Microsoft.Network/privateDnsZones@" in cosmos_pe_bicep
    assert "Microsoft.Network/privateDnsZones/virtualNetworkLinks@" in cosmos_pe_bicep
    assert "Microsoft.Network/privateEndpoints@" in cosmos_pe_bicep
    assert "Microsoft.Network/privateEndpoints/privateDnsZoneGroups@" in cosmos_pe_bicep

    # PE module wired into main.bicep with correct params
    assert "modules/cosmos-private-endpoint.bicep" in main_bicep
    assert "cosmosAccountResourceId: cosmosDb.outputs.cosmosAccountResourceId" in main_bicep
    assert "privateEndpointSubnetResourceId: network.outputs.privateEndpointSubnetResourceId" in (
        main_bicep
    )
    assert "virtualNetworkResourceId: network.outputs.virtualNetworkResourceId" in main_bicep

    # Cosmos account: governance-policy-enforced PNA: Disabled, no VNet filter
    assert "publicNetworkAccess: 'Disabled'" in cosmos_bicep
    assert "isVirtualNetworkFilterEnabled: false" in cosmos_bicep
    assert "virtualNetworkRules: []" in cosmos_bicep
    assert "param containerAppsSubnetId string" not in cosmos_bicep
    assert "containerAppsSubnetId: network.outputs.containerAppsSubnetResourceId" not in main_bicep

    # Service endpoint removed from aca-infra (PE path supersedes service endpoint)
    assert "Microsoft.AzureCosmosDB" not in network_bicep

    # IP rules retained (inert under PNA: Disabled; documented, not broadening access)
    assert "param natGatewayPublicIpAddress string" in cosmos_bicep
    assert "ipAddressOrRange: natGatewayPublicIpAddress" in cosmos_bicep
    assert "ipRules: cosmosIpRules" in cosmos_bicep
    assert "natGatewayPublicIpAddress: network.outputs.natGatewayPublicIpAddress" in main_bicep
    assert "legacyIpRule: legacyCosmosIpRule" in main_bicep
    assert '"value": "${LEGACY_COSMOS_IP_RULE=}"' in main_parameters
    assert "networkAclBypass: 'None'" in cosmos_bicep

    # Incident IP must never be hard-coded in source
    assert "20.10.253.231" not in cosmos_bicep
    assert "20.10.253.231" not in main_bicep
    assert "20.10.253.231" not in main_parameters

    # Network module retains NAT gateway, delegation, and private-endpoints subnet
    assert "Microsoft.Network/natGateways@" in network_bicep
    assert "publicIPAllocationMethod: 'Static'" in network_bicep
    assert "serviceName: 'Microsoft.App/environments'" in network_bicep
    assert "privateEndpointNetworkPolicies: 'Disabled'" in network_bicep
    assert "param infrastructureSubnetId string" in container_apps_environment_bicep
    assert "infrastructureSubnetId: infrastructureSubnetId" in container_apps_environment_bicep
    assert "workloadProfiles:" in container_apps_environment_bicep


def test_blob_storage_uses_private_endpoint_for_container_app_access() -> None:
    storage_bicep = (REPO_ROOT / "infra" / "modules" / "storage.bicep").read_text()
    network_bicep = (REPO_ROOT / "infra" / "modules" / "network.bicep").read_text()
    main_bicep = (REPO_ROOT / "infra" / "main.bicep").read_text()

    assert "publicNetworkAccess: 'Disabled'" in storage_bicep
    assert "defaultAction: 'Deny'" in storage_bicep
    assert "Microsoft.Network/privateEndpoints@" in storage_bicep
    assert "privatelink.blob.${environment().suffixes.storage}" in storage_bicep
    assert "Microsoft.Network/privateDnsZones@" in storage_bicep
    assert "Microsoft.Network/privateDnsZones/virtualNetworkLinks@" in storage_bicep
    assert "Microsoft.Network/privateEndpoints/privateDnsZoneGroups@" in storage_bicep
    assert "privateEndpointNetworkPolicies: 'Disabled'" in network_bicep
    assert "privateEndpointSubnetResourceId: network.outputs.privateEndpointSubnetResourceId" in (
        main_bicep
    )
    assert "virtualNetworkResourceId: network.outputs.virtualNetworkResourceId" in main_bicep


def test_telemetry_reuses_single_workspace_app_insights_and_secret_wiring() -> None:
    bicep_files = {
        path.relative_to(REPO_ROOT).as_posix(): path.read_text()
        for path in (REPO_ROOT / "infra").rglob("*.bicep")
    }
    all_bicep = "\n".join(bicep_files.values())
    monitoring = bicep_files["infra/modules/monitoring.bicep"]
    container_apps = bicep_files["infra/modules/container-apps.bicep"]
    main_bicep = bicep_files["infra/main.bicep"]

    assert all_bicep.count("Microsoft.OperationalInsights/workspaces@") == 1
    assert all_bicep.count("Microsoft.Insights/components@") == 1
    assert "WorkspaceResourceId: logAnalyticsWorkspace.id" in monitoring
    assert "DisableIpMasking: false" in monitoring
    assert "retentionInDays: retentionInDays" in monitoring
    assert "dailyQuotaGb: json(dailyQuotaGb)" in monitoring

    assert container_apps.count("name: 'applicationinsights-connection-string'") == 1
    assert container_apps.count("name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'") == 1
    assert "secretRef: 'applicationinsights-connection-string'" in container_apps
    assert "appInsightsResourceId: monitoring.outputs.appInsightsResourceId" in main_bicep
    assert (
        "logAnalyticsWorkspaceResourceId: monitoring.outputs.logAnalyticsWorkspaceResourceId"
        in main_bicep
    )


def test_container_app_has_all_three_dependency_free_health_probes() -> None:
    container_apps = (REPO_ROOT / "infra" / "modules" / "container-apps.bicep").read_text()

    assert container_apps.count("path: '/healthz'") == 3
    assert container_apps.count("port: 8000") >= 3
    for probe_type in ("Startup", "Liveness", "Readiness"):
        assert f"type: '{probe_type}'" in container_apps
    assert "activeRevisionsMode: 'Single'" in container_apps


def test_operational_monitoring_resources_and_alert_routing_are_iac_managed() -> None:
    operational = (REPO_ROOT / "infra" / "modules" / "operational-monitoring.bicep").read_text()
    main_bicep = (REPO_ROOT / "infra" / "main.bicep").read_text()
    main_parameters = (REPO_ROOT / "infra" / "main.parameters.json").read_text()

    assert "Microsoft.Insights/workbooks@" in operational
    assert "Microsoft.Insights/webtests@" in operational
    assert "RequestUrl: '${containerAppUrl}/healthz'" in operational
    assert "Microsoft.Insights/actionGroups@" in operational
    assert "Microsoft.Insights/scheduledQueryRules@" in operational
    assert "actionGroups:" in operational
    assert "actionGroup.id" in operational
    assert "alertsEnabled = enableAlerts && hasAlertRouting" in operational
    assert "enabled: hasAlertRouting" in operational
    assert "actionGroupEmailReceivers array = []" in operational
    assert "actionGroupWebhookReceivers array = []" in operational
    assert 'Name == "fcg.generation.requests"' in operational
    assert 'Name == "fcg.persistence.operations"' in operational
    assert 'Properties["deployment.environment.name"]' not in operational
    assert "param telemetryEnvironmentName" not in operational
    assert "by Name, Dimension, bin(TimeGenerated, 1h)" in operational
    for dimension in (
        'Properties["fcg.outcome"]',
        'Properties["fcg.moderation_reason"]',
        'Properties["fcg.attempt"]',
        'Properties["fcg.persistence_operation"]',
        'Properties["fcg.token_type"]',
    ):
        assert dimension in operational
    assert "fcg.generation.failures" not in operational
    assert "fcg.persistence.failures" not in operational

    for alert_name in (
        "availability",
        "request-failures",
        "request-latency",
        "dependency-failures",
        "exceptions",
        "generation-adverse",
        "container-restarts",
        "ingestion-cap",
    ):
        assert f"name: '{alert_name}'" in operational

    for panel_title in (
        "Requests: volume, failures, and latency percentiles",
        "Dependencies: success and latency",
        "Exceptions and bounded application errors",
        "Generation, moderation, retries, persistence, and token aggregates",
        "ACA revision, restart, and platform errors",
        "Billable ingestion and daily-cap utilization",
    ):
        assert panel_title in operational

    assert "module operationalMonitoring './modules/operational-monitoring.bicep'" in main_bicep
    assert (
        "telemetryEnvironmentName = environmentName == 'prod' ? 'production' : 'development'"
        in main_bicep
    )
    assert "telemetryEnvironmentName: telemetryEnvironmentName" in main_bicep
    assert "MONITORING_RETENTION_DAYS=30" in main_parameters
    assert "MONITORING_DAILY_QUOTA_GB=0.25" in main_parameters
    assert "TELEMETRY_SAMPLING_RATIO=1.0" in main_parameters
    assert "MONITORING_ALERTS_ENABLED=false" in main_parameters
    assert "MONITORING_REQUEST_TRAFFIC_FLOOR=5" in main_parameters
    assert "param monitoringRequestTrafficFloor int = 5" in main_bicep
    assert "param requestTrafficFloor int = 5" in operational
    assert "MONITORING_EMAIL_RECEIVERS=[]" in main_parameters
    assert "MONITORING_WEBHOOK_RECEIVERS=[]" in main_parameters

    container_apps = (REPO_ROOT / "infra" / "modules" / "container-apps.bicep").read_text()
    assert "value: 'parentbased_trace_id_ratio'" in container_apps
    assert "parentbased_traceidratio" not in container_apps
    assert "service.namespace=" not in container_apps


def test_production_container_starts_through_telemetry_first_entrypoint() -> None:
    dockerfile = (REPO_ROOT / "Dockerfile").read_text()
    pyproject = (REPO_ROOT / "pyproject.toml").read_text()

    assert '"uvicorn", "app.entrypoint:app"' in dockerfile
    assert "azure-monitor-opentelemetry" in pyproject
    assert "opentelemetry-instrumentation-httpx" in pyproject


def test_preprovision_hook_guards_session_secret() -> None:
    """Verify ensure_session_secret.sh guards APP_SESSION_SECRET_KEY before provision.

    The hook must:
    - Disable shell tracing (set +x) to prevent secret values reaching stdout/logs
    - Check the azd env for an existing APP_SESSION_SECRET_KEY before generating
    - Generate via python3 secrets module (cryptographically secure)
    - Store via `azd env set` without echoing the value
    - Unset the bash variable after storing (defence-in-depth)
    """
    hook = (REPO_ROOT / "hooks" / "ensure_session_secret.sh").read_text()

    # Secret-safe: tracing must be disabled to prevent value leaking into logs
    assert "set +x" in hook

    # Check for existing key first (idempotent / additive behaviour)
    assert "APP_SESSION_SECRET_KEY" in hook
    assert "azd env get-values" in hook

    # Cryptographically secure generation using Python's secrets module
    assert "secrets.token_hex" in hook

    # Store via azd env set (stdout suppressed so value is never echoed)
    assert "azd env set APP_SESSION_SECRET_KEY" in hook

    # Bash variable must be unset after use (defence-in-depth)
    assert "unset" in hook


def test_azd_yaml_wires_preprovision_session_secret_hook() -> None:
    """Verify azure.yaml has a preprovision hook that runs ensure_session_secret.sh.

    Without the preprovision hook, a fresh azd env (or one that has lost
    APP_SESSION_SECRET_KEY) silently passes an empty value through the
    Bicep conditional, stripping the ACA secret and env ref on the next
    azd up and causing a crash-loop.
    """
    azure_yaml = (REPO_ROOT / "azure.yaml").read_text()

    assert "preprovision:" in azure_yaml
    assert "ensure_session_secret.sh" in azure_yaml


def test_session_secret_bicep_conditional_covers_both_secret_and_env_ref() -> None:
    """Verify the Bicep conditional gates the ACA secret AND the env ref together.

    If only one of the two is gated, either:
    - The secret exists but APP_SESSION_SECRET_KEY is never injected → crash-loop, or
    - APP_SESSION_SECRET_KEY references a non-existent secret → ARM deployment failure.
    Both paths must be guarded by the same !empty(appSessionSecretKeyValue) condition.
    """
    container_apps_bicep = (REPO_ROOT / "infra" / "modules" / "container-apps.bicep").read_text()
    main_parameters = (REPO_ROOT / "infra" / "main.parameters.json").read_text()

    # Both the ACA native secret and the env secretRef are inside !empty() guards
    assert "!empty(appSessionSecretKeyValue)" in container_apps_bicep
    # The secret entry (for the secrets: [] array)
    assert "name: 'app-session-secret-key'" in container_apps_bicep
    assert "value: appSessionSecretKeyValue" in container_apps_bicep
    # The env entry (for the env: [] array)
    assert "name: 'APP_SESSION_SECRET_KEY'" in container_apps_bicep
    assert "secretRef: 'app-session-secret-key'" in container_apps_bicep

    # The azd parameter sentinel defaults to empty (not to a static value)
    # so a missing azd env var triggers the guard rather than deploying a blank secret
    assert '"value": "${APP_SESSION_SECRET_KEY=}"' in main_parameters
