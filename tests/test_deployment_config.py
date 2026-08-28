from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_dockerfile_serves_fastapi_on_port_8000() -> None:
    dockerfile = (REPO_ROOT / "Dockerfile").read_text()

    assert "FROM python:3.12-slim" in dockerfile
    assert "USER appuser" in dockerfile
    assert "EXPOSE 8000" in dockerfile
    assert '"uvicorn", "app.main:app"' in dockerfile
    assert '"--host", "0.0.0.0"' in dockerfile
    assert '"--port", "8000"' in dockerfile


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
    assert "name: 'UPSTREAM_TIMEOUT_SECONDS'" in container_apps_bicep
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
    assert "infrastructureSubnetId: infrastructureSubnetId" in (
        container_apps_environment_bicep
    )
    assert "workloadProfiles:" in container_apps_environment_bicep
