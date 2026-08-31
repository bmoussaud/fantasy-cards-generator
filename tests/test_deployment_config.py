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
    assert '"uvicorn", "app.main:app"' in dockerfile
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

    assert "mcr.microsoft.com/azuredocs/containerapps-helloworld" not in main_bicep
    assert "mcr.microsoft.com/azuredocs/containerapps-helloworld" not in container_apps_bicep
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


def test_deployer_gets_blob_data_reader_at_storage_account_scope() -> None:
    main_bicep = (REPO_ROOT / "infra" / "main.bicep").read_text()
    storage_bicep = (REPO_ROOT / "infra" / "modules" / "storage.bicep").read_text()
    storage_module = _bicep_block(main_bicep, "module storage './modules/storage.bicep'")
    deployer_assignment = _bicep_block(
        storage_bicep, "resource deployerStorageBlobDataReaderRoleAssignment"
    )
    runtime_assignment = _bicep_block(
        storage_bicep, "resource storageBlobDataContributorRoleAssignment"
    )

    assert "deployerPrincipalId: deployerPrincipalId" in storage_module
    assert "deployerPrincipalType: deployerPrincipalType" in storage_module
    assert (
        "var storageBlobDataReaderRoleDefinitionId = "
        "'2a2b9908-6ea1-4ae2-8e65-a410df84e7d1'" in storage_bicep
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


def test_cosmos_account_explicitly_keeps_public_network_access_for_mvp() -> None:
    cosmos_bicep = (REPO_ROOT / "infra" / "modules" / "cosmos-db.bicep").read_text()
    main_bicep = (REPO_ROOT / "infra" / "main.bicep").read_text()
    main_parameters = (REPO_ROOT / "infra" / "main.parameters.json").read_text()
    network_bicep = (REPO_ROOT / "infra" / "modules" / "network.bicep").read_text()
    container_apps_environment_bicep = (
        REPO_ROOT / "infra" / "modules" / "container-apps-environment.bicep"
    ).read_text()

    assert "param natGatewayPublicIpAddress string" in cosmos_bicep
    assert "ipAddressOrRange: natGatewayPublicIpAddress" in cosmos_bicep
    assert "ipRules: cosmosIpRules" in cosmos_bicep
    assert "natGatewayPublicIpAddress: network.outputs.natGatewayPublicIpAddress" in main_bicep
    assert "legacyIpRule: legacyCosmosIpRule" in main_bicep
    assert '"value": "${LEGACY_COSMOS_IP_RULE=}"' in main_parameters
    assert "isVirtualNetworkFilterEnabled: false" in cosmos_bicep
    assert "networkAclBypass: 'None'" in cosmos_bicep
    assert "networkAclBypassResourceIds: []" in cosmos_bicep
    assert "publicNetworkAccess: 'Enabled'" in cosmos_bicep
    assert "virtualNetworkRules: []" in cosmos_bicep
    assert "20.10.253.231" not in cosmos_bicep
    assert "20.10.253.231" not in main_bicep
    assert "20.10.253.231" not in main_parameters
    assert "Microsoft.Network/natGateways@" in network_bicep
    assert "publicIPAllocationMethod: 'Static'" in network_bicep
    assert "serviceName: 'Microsoft.App/environments'" in network_bicep
    assert "param infrastructureSubnetId string" in container_apps_environment_bicep
    assert "infrastructureSubnetId: infrastructureSubnetId" in (container_apps_environment_bicep)
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
