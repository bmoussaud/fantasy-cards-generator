import json
import subprocess
from pathlib import Path

import pytest

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


def _compile_bicep(path: Path) -> dict:
    result = subprocess.run(
        ["az", "bicep", "build", "--file", str(path), "--stdout"],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


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
    assert "name: 'FOUNDRY_PROJECT_ENDPOINT'" in container_apps_bicep
    assert "name: 'FOUNDRY_TEXT_DEPLOYMENT'" in container_apps_bicep
    assert "name: 'FOUNDRY_IMAGE_DEPLOYMENT'" in container_apps_bicep
    assert "name: 'COSMOS_ENDPOINT'" in container_apps_bicep
    assert "name: 'COSMOS_DATABASE_NAME'" in container_apps_bicep
    assert "name: 'COSMOS_CONTAINER_NAME'" in container_apps_bicep
    assert "name: 'BLOB_ENDPOINT'" in container_apps_bicep
    assert "name: 'BLOB_CONTAINER_NAME'" in container_apps_bicep
    assert "name: 'PROFILE_PHOTOS_CONTAINER_NAME'" in container_apps_bicep
    assert "name: 'CONTENT_SAFETY_ENDPOINT'" in container_apps_bicep
    assert "name: 'CONTENT_SAFETY_API_VERSION'" in container_apps_bicep
    assert "name: 'CONTENT_SAFETY_MAX_HATE_SEVERITY'" in container_apps_bicep
    assert "name: 'CONTENT_SAFETY_MAX_SELF_HARM_SEVERITY'" in container_apps_bicep
    assert "name: 'CONTENT_SAFETY_MAX_SEXUAL_SEVERITY'" in container_apps_bicep
    assert "name: 'CONTENT_SAFETY_MAX_VIOLENCE_SEVERITY'" in container_apps_bicep
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
    assert "name: 'SAVED_PHOTO_MAX_COUNT'" in container_apps_bicep
    assert "name: 'SAVED_PHOTO_MAX_BYTES'" in container_apps_bicep
    assert "name: 'SAVED_PHOTO_THUMBNAIL_SIZE'" in container_apps_bicep

    assert (
        "foundryEndpoint: 'https://${aiFoundryAccountName}.cognitiveservices.azure.com/'"
        in main_bicep
    )
    assert "foundryProjectEndpoint: aiFoundryProjectEndpoint" in main_bicep
    assert "contentSafetyEndpoint: resolvedContentSafetyEndpoint" in main_bicep
    assert "foundryTextDeployment: aiFoundryTextDeploymentName" in main_bicep
    assert "foundryImageDeployment: aiFoundryImageDeploymentName" in main_bicep
    assert "cosmosEndpoint: 'https://${cosmosAccountName}.documents.azure.com:443/'" in main_bicep
    assert (
        "blobEndpoint: 'https://${storageAccountName}.blob.${environment().suffixes.storage}/'"
        in main_bicep
    )
    assert "trustedProxyHops: trustedProxyHops" in main_bicep


def test_foundry_project_endpoint_and_agent_access_gate_are_iac_managed() -> None:
    main_bicep = (REPO_ROOT / "infra" / "main.bicep").read_text()
    main_parameters = (REPO_ROOT / "infra" / "main.parameters.json").read_text()
    foundry_bicep = (REPO_ROOT / "infra" / "modules" / "ai-foundry.bicep").read_text()
    container_apps_bicep = (REPO_ROOT / "infra" / "modules" / "container-apps.bicep").read_text()
    readme = (REPO_ROOT / "infra" / "README.md").read_text()
    foundry_module = _bicep_block(main_bicep, "module aiFoundry './modules/ai-foundry.bicep'")
    container_apps_module = _bicep_block(
        main_bicep, "module containerApps './modules/container-apps.bicep'"
    )
    direct_model_assignment = _bicep_block(
        foundry_bicep, "resource cognitiveServicesUserRoleAssignment"
    )
    project_identity_assignment = _bicep_block(
        foundry_bicep, "resource projectManagedIdentityFoundryUserRoleAssignment"
    )
    agent_consumer_assignment = _bicep_block(
        foundry_bicep, "resource containerAppFoundryAgentConsumerRoleAssignment"
    )

    assert "param enableFoundryAgentAccess bool = false" in main_bicep
    assert "param enableFoundryAgentAccess bool = false" in foundry_bicep
    assert (
        '"enableFoundryAgentAccess": {\n'
        '      "value": "${ENABLE_FOUNDRY_AGENT_ACCESS=false}"' in main_parameters
    )
    assert "enableFoundryAgentAccess: enableFoundryAgentAccess" in foundry_module

    assert (
        "var resolvedAiFoundryProjectName = "
        "take('${aiFoundryProjectName}-${environmentName}', 64)" in main_bicep
    )
    assert "projectName: resolvedAiFoundryProjectName" in foundry_module
    assert (
        "var aiFoundryProjectEndpoint = "
        "'https://${aiFoundryAccountName}.services.ai.azure.com/api/projects/${resolvedAiFoundryProjectName}'"
        in main_bicep
    )
    assert "foundryProjectEndpoint: aiFoundryProjectEndpoint" in container_apps_module
    assert "param foundryProjectEndpoint string = ''" in container_apps_bicep
    assert "name: 'FOUNDRY_PROJECT_ENDPOINT'" in container_apps_bicep
    assert "value: foundryProjectEndpoint" in container_apps_bicep
    assert "secretRef: 'foundry-project-endpoint'" not in container_apps_bicep
    assert (
        "output aiFoundryProjectEndpoint string = "
        "'https://${foundryAccount.name}.services.ai.azure.com/api/projects/${aiFoundryProject.name}'"
        in foundry_bicep
    )
    assert (
        "output aiFoundryProjectEndpoint string = "
        "aiFoundry.outputs.aiFoundryProjectEndpoint" in main_bicep
    )

    # The project endpoint is computed from root naming and injected before the
    # Foundry module output exists, avoiding a Container App <-> Foundry cycle.
    assert "aiFoundry.outputs.aiFoundryProjectEndpoint" not in container_apps_module
    assert "containerApps.outputs" not in container_apps_module

    # Existing direct model access stays untouched and is not gated by the new
    # hosted-agent access flag.
    assert "if (enableFoundryAgentAccess)" not in direct_model_assignment
    assert "scope: foundryAccount" in direct_model_assignment
    assert "principalId: containerAppPrincipalId" in direct_model_assignment
    assert "cognitiveServicesUserRoleDefinitionId" in direct_model_assignment

    assert (
        "var foundryUserRoleDefinitionId = '53ca6127-db72-4b80-b1b0-d745d6d5456d'" in foundry_bicep
    )
    assert (
        "var foundryAgentConsumerRoleDefinitionId = 'eed3b665-ab3a-47b6-8f48-c9382fb1dad6'"
        in foundry_bicep
    )
    assert "if (enableFoundryAgentAccess)" in project_identity_assignment
    assert "scope: foundryAccount" in project_identity_assignment
    assert "principalId: aiFoundryProject.identity.principalId" in project_identity_assignment
    assert "containerAppPrincipalId" not in project_identity_assignment
    assert "principalType: 'ServicePrincipal'" in project_identity_assignment
    assert (
        "guid(foundryAccount.id, aiFoundryProject.id, foundryUserRoleDefinitionId)"
        in project_identity_assignment
    )
    assert "foundryUserRoleDefinitionId" in project_identity_assignment
    assert "if (enableFoundryAgentAccess)" in agent_consumer_assignment
    assert "scope: aiFoundryProject" in agent_consumer_assignment
    assert "principalId: containerAppPrincipalId" in agent_consumer_assignment
    assert "principalType: 'ServicePrincipal'" in agent_consumer_assignment
    assert "foundryUserRoleDefinitionId" not in agent_consumer_assignment
    assert (
        "guid(aiFoundryProject.id, containerAppPrincipalId, foundryAgentConsumerRoleDefinitionId)"
        in agent_consumer_assignment
    )
    assert "foundryAgentConsumerRoleDefinitionId" in agent_consumer_assignment

    assert "ENABLE_FOUNDRY_AGENT_ACCESS" in readme
    assert "FOUNDRY_PROJECT_ENDPOINT" in readme
    assert "generation modes" in readme
    assert "create agents" in readme
    assert "Foundry Agent" in readme and "Consumer" in readme


def test_dev_text_model_capacity_is_persisted_and_has_a_targeted_leaf() -> None:
    main_bicep = (REPO_ROOT / "infra" / "main.bicep").read_text()
    foundry_bicep = (REPO_ROOT / "infra" / "modules" / "ai-foundry.bicep").read_text()
    capacity_leaf = (REPO_ROOT / "infra" / "text-model-capacity.bicep").read_text()
    foundry_module = _bicep_block(main_bicep, "module aiFoundry './modules/ai-foundry.bicep'")
    text_deployment = _bicep_block(foundry_bicep, "resource textModelDeployment")

    assert (
        "param aiFoundryTextDeploymentCapacity int = environmentName == 'dev' ? 10 : 1"
        in main_bicep
    )
    assert "textDeploymentCapacity: aiFoundryTextDeploymentCapacity" in foundry_module
    assert "textDeploymentRaiPolicyName: aiFoundryTextDeploymentRaiPolicyName" in foundry_module
    assert (
        "textDeploymentVersionUpgradeOption: "
        "aiFoundryTextDeploymentVersionUpgradeOption" in foundry_module
    )
    assert "raiPolicyName: textDeploymentRaiPolicyName" in text_deployment
    assert "versionUpgradeOption: textDeploymentVersionUpgradeOption" in text_deployment
    assert "param " not in capacity_leaf
    assert "var targetIsExactDev" in capacity_leaf
    assert ": fail('Refusing model-capacity update:" in capacity_leaf
    assert "resource foundryAccount" in capacity_leaf and "existing = {" in capacity_leaf
    assert capacity_leaf.count("Microsoft.CognitiveServices/accounts/deployments") == 1
    assert "var textDeploymentName = 'gpt-5-5'" in capacity_leaf
    assert "capacity: 10" in capacity_leaf
    assert "name: 'GlobalStandard'" in capacity_leaf
    assert "name: 'gpt-5.5'" in capacity_leaf
    assert "version: '2026-04-24'" in capacity_leaf
    assert "raiPolicyName: 'Microsoft.DefaultV2'" in capacity_leaf
    assert "versionUpgradeOption: 'OnceNewDefaultVersionAvailable'" in capacity_leaf


def test_dev_text_model_capacity_compiles_to_exact_fail_closed_target() -> None:
    compiled = _compile_bicep(REPO_ROOT / "infra" / "text-model-capacity.bicep")

    assert "parameters" not in compiled
    variables = compiled["variables"]
    assert variables["devArmDeploymentName"] == "dev-text-model-capacity-10"
    assert variables["devFoundryAccountName"] == "aifcagdevqhg3qc4rlbt4g"
    assert variables["devResourceGroupName"] == "rg-fcag-dev"
    assert variables["devSubscriptionId"] == "b8ff3e15-7e2d-4fac-a773-992fb59ccedd"
    assert variables["textDeploymentName"] == "gpt-5-5"
    assert variables["subscriptionIsExactDev"] == (
        "[equals(subscription().subscriptionId, variables('devSubscriptionId'))]"
    )
    assert variables["resourceGroupIsExactDev"] == (
        "[equals(resourceGroup().name, variables('devResourceGroupName'))]"
    )
    assert variables["armDeploymentNameIsExact"] == (
        "[equals(deployment().name, variables('devArmDeploymentName'))]"
    )
    assert variables["targetIsExactDev"] == (
        "[and(and(variables('subscriptionIsExactDev'), "
        "variables('resourceGroupIsExactDev')), "
        "variables('armDeploymentNameIsExact'))]"
    )
    assert variables["validatedTarget"] == (
        "[if(variables('targetIsExactDev'), "
        "createObject('accountName', variables('devFoundryAccountName'), "
        "'deploymentName', variables('textDeploymentName')), "
        "fail('Refusing model-capacity update: target must be the exact "
        "approved dev subscription, resource group, and ARM deployment name.'))]"
    )

    deployment = compiled["resources"][0]
    assert deployment["name"] == (
        "[format('{0}/{1}', variables('validatedTarget').accountName, "
        "variables('validatedTarget').deploymentName)]"
    )
    assert deployment["sku"] == {"capacity": 10, "name": "GlobalStandard"}
    assert deployment["properties"] == {
        "model": {
            "format": "OpenAI",
            "name": "gpt-5.5",
            "version": "2026-04-24",
        },
        "raiPolicyName": "Microsoft.DefaultV2",
        "versionUpgradeOption": "OnceNewDefaultVersionAvailable",
    }

    expected_dev_context = {
        "subscriptionId": variables["devSubscriptionId"],
        "resourceGroupName": variables["devResourceGroupName"],
        "armDeploymentName": variables["devArmDeploymentName"],
        "accountName": variables["devFoundryAccountName"],
    }

    def resolve_compiled_target(context: dict[str, str]) -> tuple[str, str]:
        guarded_context = {
            "subscriptionId": context["subscriptionId"],
            "resourceGroupName": context["resourceGroupName"],
            "armDeploymentName": context["armDeploymentName"],
        }
        expected_guarded_context = {
            "subscriptionId": expected_dev_context["subscriptionId"],
            "resourceGroupName": expected_dev_context["resourceGroupName"],
            "armDeploymentName": expected_dev_context["armDeploymentName"],
        }
        if guarded_context != expected_guarded_context:
            raise ValueError("fail() rejects a non-dev deployment context")
        if context["accountName"] != expected_dev_context["accountName"]:
            raise ValueError("the compiled template exposes no account override")
        return (
            variables["devFoundryAccountName"],
            variables["textDeploymentName"],
        )

    assert resolve_compiled_target(expected_dev_context) == (
        "aifcagdevqhg3qc4rlbt4g",
        "gpt-5-5",
    )
    for field, production_like_value in (
        ("subscriptionId", "00000000-0000-0000-0000-000000000000"),
        ("resourceGroupName", "rg-fcag-prod"),
        ("armDeploymentName", "prod-text-model-capacity-10"),
        ("accountName", "aifcagprod000000000000"),
    ):
        production_like_context = expected_dev_context | {field: production_like_value}
        with pytest.raises(ValueError):
            resolve_compiled_target(production_like_context)


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
    profile_runtime_assignment = _bicep_block(
        storage_bicep, "resource profilePhotosBlobDataContributorRoleAssignment"
    )

    assert "deployerPrincipalId: deployerPrincipalId" in storage_module
    assert "deployerPrincipalType: deployerPrincipalType" in storage_module
    assert "profilePhotosContainerName: profilePhotosContainerName" in storage_module
    assert (
        "var storageBlobDataReaderRoleDefinitionId = "
        "'2a2b9908-6ea1-4ae2-8e65-a410df84e7d1'" in storage_bicep
    )
    assert "storageBlobDelegatorRoleDefinitionId" not in storage_bicep
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
    assert "principalId: containerAppPrincipalId" in profile_runtime_assignment
    assert "principalType: 'ServicePrincipal'" in profile_runtime_assignment
    assert "storageBlobDataContributorRoleDefinitionId" in profile_runtime_assignment
    assert "scope: profilePhotosContainer" in profile_runtime_assignment
    assert "scope: storageAccount" not in profile_runtime_assignment
    assert (
        "guid(profilePhotosContainer.id, containerAppPrincipalId, "
        "storageBlobDataContributorRoleDefinitionId)" in profile_runtime_assignment
    )


def test_deployer_gets_key_vault_reader_at_vault_scope() -> None:
    main_bicep = (REPO_ROOT / "infra" / "main.bicep").read_text()
    main_parameters = json.loads((REPO_ROOT / "infra" / "main.parameters.json").read_text())
    security_bicep = (REPO_ROOT / "infra" / "modules" / "security.bicep").read_text()
    security_module = _bicep_block(main_bicep, "module security './modules/security.bicep'")
    deployer_assignment = _bicep_block(
        security_bicep, "resource deployerKeyVaultReaderRoleAssignment"
    )
    runtime_assignment = _bicep_block(
        main_bicep, "resource containerAppKeyVaultSecretsUserRoleAssignment"
    )

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
    assert "principalId: containerApps.outputs.containerAppPrincipalId" in runtime_assignment
    assert "principalType: 'ServicePrincipal'" in runtime_assignment
    assert "keyVaultSecretsUserRoleDefinitionId" in runtime_assignment
    assert "keyVaultAccessPrincipalId" not in security_bicep
    assert "keyVaultAccessPrincipalId" not in security_module
    assert "name: keyVaultName" in main_bicep
    assert (
        "guid(keyVault.id, containerAppName, keyVaultSecretsUserRoleDefinitionId)"
        in runtime_assignment
    )


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
    assert '"value": "${CONTENT_SAFETY_ENDPOINT=}"' in main_parameters
    assert '"value": "${CONTENT_SAFETY_API_VERSION=2024-09-01}"' in main_parameters
    assert '"value": "${SAVED_PHOTO_MAX_COUNT=10}"' in main_parameters
    assert '"value": "${SAVED_PHOTO_MAX_BYTES=4194304}"' in main_parameters
    assert '"value": "${SAVED_PHOTO_THUMBNAIL_SIZE=200}"' in main_parameters

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
    assert "profile-photos" in readme
    assert "CONTENT_SAFETY_ENDPOINT" in readme
    assert "readiness every 10s" in readme
    assert "liveness every 30s" in readme


def test_container_apps_wire_key_vault_backed_auth_env_vars() -> None:
    container_apps_bicep = (REPO_ROOT / "infra" / "modules" / "container-apps.bicep").read_text()
    security_bicep = (REPO_ROOT / "infra" / "modules" / "security.bicep").read_text()
    main_bicep = (REPO_ROOT / "infra" / "main.bicep").read_text()
    main_parameters = (REPO_ROOT / "infra" / "main.parameters.json").read_text()

    # APP_SESSION_SECRET_KEY and ENTRA_CLIENT_SECRET remain in Key Vault only;
    # the Container App gets non-secret provider config instead of copied values.
    assert "name: 'APP_SESSION_SECRET_KEY'" not in container_apps_bicep
    assert "secretRef: 'app-session-secret-key'" not in container_apps_bicep
    assert "name: 'ENTRA_CLIENT_SECRET'" not in container_apps_bicep
    assert "secretRef: 'entra-client-secret'" not in container_apps_bicep
    assert "name: 'app-session-secret-key'" not in container_apps_bicep
    assert "name: 'entra-client-secret'" not in container_apps_bicep
    assert "param appSessionSecretKeyValue" not in container_apps_bicep
    assert "param entraClientSecretValue" not in container_apps_bicep
    assert "keyVaultUrl:" not in container_apps_bicep
    assert "appSessionSecretKeySecretUri" not in container_apps_bicep
    assert "entraClientSecretSecretUri" not in container_apps_bicep

    assert "name: 'SECRET_PROVIDER_BACKEND'" in container_apps_bicep
    assert "value: keyVaultProviderBackend" in container_apps_bicep
    assert "name: 'KEY_VAULT_URI'" in container_apps_bicep
    assert "value: keyVaultUri" in container_apps_bicep
    assert "name: 'SECRET_PROVIDER_CACHE_TTL_SECONDS'" in container_apps_bicep
    assert "name: 'SECRET_PROVIDER_REQUEST_TIMEOUT_SECONDS'" in container_apps_bicep
    assert "name: 'SECRET_PROVIDER_MAX_RETRIES'" in container_apps_bicep
    assert "name: 'SECRET_PROVIDER_RETRY_BACKOFF_SECONDS'" in container_apps_bicep
    assert "name: 'SECRET_PROVIDER_MAX_STALE_SECONDS'" in container_apps_bicep

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
    # params for Key Vault persistence instead of hardcoding plaintext.
    assert "@secure()" in main_bicep
    assert "param appSessionSecretKeyValue string = ''" in main_bicep
    assert "param entraClientSecretValue string = ''" in main_bicep
    assert '"value": "${APP_SESSION_SECRET_KEY=}"' in main_parameters
    assert '"value": "${ENTRA_CLIENT_SECRET=}"' in main_parameters
    assert '"value": "${SECRET_PROVIDER_BACKEND=azure}"' in main_parameters
    assert '"value": "${SECRET_PROVIDER_MAX_STALE_SECONDS=300}"' in main_parameters

    # Key Vault still stores the provisioned secrets.
    assert "Microsoft.KeyVault/vaults/secrets" in security_bicep
    assert "'app-session-secret-key'" in security_bicep
    assert "'entra-client-secret'" in security_bicep

    # The deployed redirect URI must be derived from the Container Apps
    # environment domain, not depend on the container app's own output
    # (which would create a circular module dependency).
    assert "deployedAuthRedirectUri" in main_bicep
    assert "containerAppsEnvironmentDefaultDomain" in main_bicep


def test_entra_client_id_override_wires_false_mode_redeploy() -> None:
    """Existing-registration redeploys must validate the override before updating ACA."""
    main_bicep = (REPO_ROOT / "infra" / "main.bicep").read_text()
    main_parameters = (REPO_ROOT / "infra" / "main.parameters.json").read_text()

    # Override param must exist with an empty-string default (non-breaking for managed mode).
    assert "param entraClientIdOverride string = ''" in main_bicep

    # Reading the raw override would bypass the guard's deployment dependency.
    old_ternary = (
        "var entraClientId = deployEntraAppRegistration"
        " ? appRegistration!.outputs.appId : entraClientIdOverride"
    )
    assert old_ternary not in main_bicep, (
        "Old two-branch ternary still present — silent auth erase not fixed. "
        "Expected three-branch expression routing through guard for existing mode."
    )

    # Three-branch entraClientId resolution must be present:
    # managed → appRegistration!.outputs.appId (unchanged)
    # existing → entraAuthGuard!.outputs.validatedClientId (forces ARM dependency on guard)
    # disabled → '' (explicit opt-in, no guard needed)
    assert "var entraClientId = deployEntraAppRegistration" in main_bicep
    assert "appRegistration!.outputs.appId" in main_bicep
    assert "entraAuthGuard!.outputs.validatedClientId" in main_bicep
    assert "entraAuthMode == 'existing'" in main_bicep

    # Guard module deployed only when false+existing — fail-closed sentinel.
    expected_guard_condition = (
        "module entraAuthGuard './modules/entra-auth-guard.bicep'"
        " = if (!deployEntraAppRegistration && entraAuthMode == 'existing')"
    )
    assert expected_guard_condition in main_bicep

    # Redirect URIs still guarded on entraClientId being non-empty (not on the flag).
    assert "empty(entraClientId) ? '' : deployedPostLogoutRedirectUri" in main_bicep
    assert "empty(entraClientId) ? '' : deployedAuthRedirectUri" in main_bicep
    assert "deployEntraAppRegistration ? deployedPostLogoutRedirectUri : ''" not in main_bicep
    assert "deployEntraAppRegistration ? deployedAuthRedirectUri : ''" not in main_bicep

    # parameters.json must wire both override and auth-mode from environment.
    assert '"entraClientIdOverride"' in main_parameters
    assert '"value": "${ENTRA_CLIENT_ID_OVERRIDE=}"' in main_parameters
    assert '"entraAuthMode"' in main_parameters
    assert '"value": "${ENTRA_AUTH_MODE=existing}"' in main_parameters


def test_entra_false_mode_redirect_uris_computed_not_hardcoded() -> None:
    """Redirect URIs are deterministic from the ACA environment domain.

    They must not be suppressed by the deployEntraAppRegistration flag when
    a client ID override is present.  The container-apps module receives computed
    URIs whenever entraClientId is non-empty, regardless of the registration mode.
    """
    main_bicep = (REPO_ROOT / "infra" / "main.bicep").read_text()

    # deployedAuthRedirectUri is constructed from the ACA environment domain — always.
    assert "var deployedAuthRedirectUri = " in main_bicep
    assert "var deployedPostLogoutRedirectUri = " in main_bicep

    # The guard that gates redirect URI on client ID presence must appear exactly once each.
    assert main_bicep.count("empty(entraClientId) ? '' : deployedAuthRedirectUri") == 1
    assert main_bicep.count("empty(entraClientId) ? '' : deployedPostLogoutRedirectUri") == 1


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


def test_key_vault_private_endpoint_enables_container_app_vault_access() -> None:
    kv_pe_bicep = (REPO_ROOT / "infra" / "modules" / "keyvault-private-endpoint.bicep").read_text()
    main_bicep = (REPO_ROOT / "infra" / "main.bicep").read_text()
    security_bicep = (REPO_ROOT / "infra" / "modules" / "security.bicep").read_text()
    dns_module = _bicep_block(kv_pe_bicep, "module kvPrivateDnsZone ")
    endpoint_module = _bicep_block(kv_pe_bicep, "module kvPrivateEndpoint ")
    root_module = _bicep_block(main_bicep, "module keyVaultPrivateEndpoint ")

    assert "br/public:avm/res/network/private-dns-zone:" in dns_module
    assert "br/public:avm/res/network/private-endpoint:" in endpoint_module
    assert "privatelink.vaultcore.azure.net" in kv_pe_bicep
    assert "name: kvPrivateDnsZoneName" in dns_module
    assert "virtualNetworkLinks:" in dns_module
    assert "virtualNetworkResourceId: virtualNetworkResourceId" in dns_module
    assert "registrationEnabled: false" in dns_module
    assert "groupIds: [\n            'vault'\n          ]" in endpoint_module
    assert "privateLinkServiceId: keyVaultResourceId" in endpoint_module
    assert "subnetResourceId: privateEndpointSubnetResourceId" in endpoint_module
    assert "privateDnsZoneGroup:" in endpoint_module
    assert "privateDnsZoneResourceId: kvPrivateDnsZone.outputs.resourceId" in endpoint_module
    assert "enableTelemetry: false" in dns_module
    assert "enableTelemetry: false" in endpoint_module

    assert "modules/keyvault-private-endpoint.bicep" in root_module
    assert "keyVaultResourceId: security.outputs.keyVaultResourceId" in root_module
    assert "keyVaultName: security.outputs.keyVaultName" in root_module
    assert (
        "privateEndpointSubnetResourceId: network.outputs.privateEndpointSubnetResourceId"
        in root_module
    )
    assert "virtualNetworkResourceId: network.outputs.virtualNetworkResourceId" in root_module
    assert "output keyVaultResourceId string" in security_bicep
    assert "publicNetworkAccess: 'Disabled'" in security_bicep
    assert "containerApps" not in root_module
    assert "keyVaultSecretsUserRoleDefinitionId" in main_bicep
    assert "'4633458b-17de-408a-b874-0445c86b69e6'" in main_bicep


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
    assert monitoring.count("resource applicationInsights 'Microsoft.Insights/components@") == 1
    assert (
        bicep_files["infra/modules/ai-foundry.bicep"].count(
            "resource appInsights 'Microsoft.Insights/components@2020-02-02' existing"
        )
        == 1
    )
    assert "WorkspaceResourceId: logAnalyticsWorkspace.id" in monitoring
    assert "DisableIpMasking: false" in monitoring
    assert "retentionInDays: retentionInDays" in monitoring
    assert "dailyQuotaGb: json(dailyQuotaGb)" in monitoring

    assert container_apps.count("name: 'applicationinsights-connection-string'") == 1
    assert container_apps.count("name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'") == 1
    assert "secretRef: 'applicationinsights-connection-string'" in container_apps
    assert container_apps.count("name: 'AZURE_EXPERIMENTAL_ENABLE_GENAI_TRACING'") == 1
    assert "name: 'AZURE_EXPERIMENTAL_ENABLE_GENAI_TRACING'\n      value: 'true'" in container_apps
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


def test_session_secret_flows_only_into_key_vault_not_container_app() -> None:
    """Verify APP_SESSION_SECRET_KEY is provisioned only into Key Vault.

    Runtime retrieval now happens through the app's managed identity, so the
    Container App template must not keep a mirrored ACA-native secret copy.
    """
    container_apps_bicep = (REPO_ROOT / "infra" / "modules" / "container-apps.bicep").read_text()
    security_bicep = (REPO_ROOT / "infra" / "modules" / "security.bicep").read_text()
    main_parameters = (REPO_ROOT / "infra" / "main.parameters.json").read_text()

    assert "name: 'app-session-secret-key'" not in container_apps_bicep
    assert "name: 'APP_SESSION_SECRET_KEY'" not in container_apps_bicep
    assert "secretRef: 'app-session-secret-key'" not in container_apps_bicep
    assert "name: 'app-session-secret-key'" in security_bicep
    assert "value: appSessionSecretKeyValue" in security_bicep

    # The azd parameter sentinel defaults to empty (not to a static value)
    # so a missing azd env var skips the Key Vault secret instead of hardcoding one.
    assert '"value": "${APP_SESSION_SECRET_KEY=}"' in main_parameters


# ── Consolidated root entry point (issue #130) ──────────────────────


def test_root_manifest_declares_both_services() -> None:
    """Root azure.yaml is the single manifest for web-nat and card-orchestrator."""
    azure_yaml = (REPO_ROOT / "azure.yaml").read_text()

    assert "web-nat:" in azure_yaml
    assert "host: containerapp" in azure_yaml
    assert "card-orchestrator:" in azure_yaml
    assert "host: azure.ai.agent" in azure_yaml
    assert "kind: hosted" in azure_yaml
    assert "name: card-orchestrator" in azure_yaml
    assert "port: 8000" in azure_yaml


def test_root_manifest_requires_azd_version_and_agent_extension() -> None:
    azure_yaml = (REPO_ROOT / "azure.yaml").read_text()

    assert "requiredVersions:" in azure_yaml
    assert ">= 1.32.0" in azure_yaml
    assert "azure.ai.agents:" in azure_yaml
    assert "=1.0.0-beta.13" in azure_yaml


def test_root_manifest_safe_default_workflow_deploys_only_web() -> None:
    """The default up workflow provisions and deploys only web-nat.

    The card-orchestrator hosted agent is never built, pushed, or deployed by
    ``azd up``.  An operator must explicitly run ``azd deploy card-orchestrator``
    after enabling prerequisites and reviewing the agent deployment.
    """
    azure_yaml = (REPO_ROOT / "azure.yaml").read_text()

    assert "workflows:" in azure_yaml
    assert "deploy web-nat" in azure_yaml
    # Safety: the default up workflow must never deploy all services
    assert "deploy --all" not in azure_yaml
    # The workflows section must not contain a card-orchestrator deploy step
    workflows_start = azure_yaml.index("workflows:")
    hooks_start = azure_yaml.index("hooks:")
    workflows_section = azure_yaml[workflows_start:hooks_start]
    assert "card-orchestrator" not in workflows_section


def test_root_manifest_hooks_only_on_provision_not_deploy() -> None:
    """Hooks run only during provision; deploy-only paths do not rotate credentials."""
    azure_yaml = (REPO_ROOT / "azure.yaml").read_text()

    assert "preprovision:" in azure_yaml
    assert "postprovision:" in azure_yaml
    # No deploy hooks that could trigger credential rotation on azd deploy
    assert "predeploy:" not in azure_yaml
    assert "postdeploy:" not in azure_yaml


def test_card_orchestrator_docker_paths_are_root_relative() -> None:
    """Root-relative Docker paths resolve to the same Dockerfile as the nested manifest."""
    azure_yaml = (REPO_ROOT / "azure.yaml").read_text()

    assert "hosted_agents/card_orchestrator/Dockerfile" in azure_yaml
    assert (REPO_ROOT / "hosted_agents/card_orchestrator/Dockerfile").is_file()
    # Port 8088 is the hosted agent container port (in the Dockerfile), not in
    # the root manifest resources.  Port 8000 is the web service.
    assert "EXPOSE 8088" in (REPO_ROOT / "hosted_agents/card_orchestrator/Dockerfile").read_text()


def test_card_orchestrator_prerequisites_default_off_in_root_bicep() -> None:
    """Hosted-agent prerequisites are off by default; web-only workflow is safe."""
    main = (REPO_ROOT / "infra/main.bicep").read_text()
    params = json.loads((REPO_ROOT / "infra/main.parameters.json").read_text())["parameters"]

    assert "param enableCardOrchestratorPrerequisites bool = false" in main
    assert "param createRegistryConnection bool = false" in main
    assert "= if (enableCardOrchestratorPrerequisites)" in main
    assert params["enableCardOrchestratorPrerequisites"]["value"].endswith("=false}")
    assert params["createRegistryConnection"]["value"].endswith("=false}")
    assert params["enableAgentAlerts"]["value"].endswith("=false}")


def test_root_bicep_exports_hosted_agent_extension_outputs() -> None:
    """Root Bicep provides all outputs the azure.ai.agent extension needs."""
    main = (REPO_ROOT / "infra/main.bicep").read_text()

    for output_name in (
        "AZURE_AI_PROJECT_ID",
        "AZURE_AI_PROJECT_ENDPOINT",
        "FOUNDRY_PROJECT_ENDPOINT",
        "AZURE_AI_ACCOUNT_NAME",
        "AZURE_AI_PROJECT_NAME",
        "AZURE_CONTAINER_REGISTRY_RESOURCE_ID",
        "AZURE_AI_PROJECT_ACR_CONNECTION_NAME",
    ):
        assert f"output {output_name}" in main


def test_root_agent_prerequisites_use_canonical_acr_pull_role_and_connection() -> None:
    """Root agent-prerequisites module uses the same canonical ACR role ID and
    registry-connection pattern as the card-orchestrator repair facade."""
    prereqs = (REPO_ROOT / "infra/modules/agent-prerequisites.bicep").read_text()

    # Canonical AcrPull role ID
    assert "'7f951dda-4ed3-4680-a7ca-43fe172d538d'" in prereqs
    assert "guid(registry.id, projectPrincipalId, acrPullRoleDefinitionId)" in prereqs
    # Registry connection contract
    assert "category: 'ContainerRegistry'" in prereqs
    assert "authType: 'ManagedIdentity'" in prereqs
    assert "clientId: aiFoundryProject.identity.principalId" in prereqs
    assert "resourceId: registry.id" in prereqs
    assert "= if (createRegistryConnection)" in prereqs
    # No duplicate Foundry RBAC — those are in ai-foundry.bicep
    assert "foundryUserRoleDefinitionId" not in prereqs
    assert "foundryAgentConsumerRoleDefinitionId" not in prereqs


def test_root_agent_monitoring_is_gated_and_alerts_default_off() -> None:
    """Agent monitoring deploys only when prerequisites are enabled; alerts default off."""
    main = (REPO_ROOT / "infra/main.bicep").read_text()
    params = json.loads((REPO_ROOT / "infra/main.parameters.json").read_text())["parameters"]

    assert "module agentMonitoring" in main
    assert "enableAgentAlerts" in main
    assert "param enableAgentAlerts bool = false" in main
    assert params["enableAgentAlerts"]["value"] == "${CARD_ORCHESTRATOR_ENABLE_AGENT_ALERTS=false}"


def test_nested_manifest_is_deprecated() -> None:
    """The deployments/card-orchestrator/azure.yaml has a deprecation notice."""
    content = (REPO_ROOT / "deployments/card-orchestrator/azure.yaml").read_text()

    assert "DEPRECATED" in content


def test_nested_launcher_is_deprecated() -> None:
    """The deployments/card-orchestrator/deploy.py has a deprecation notice."""
    content = (REPO_ROOT / "deployments/card-orchestrator/deploy.py").read_text()

    assert "DEPRECATED" in content
