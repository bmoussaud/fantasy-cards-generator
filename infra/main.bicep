@description('Deployment location for all resources.')
param location string = resourceGroup().location

@description('Target environment name. Only dev and prod are supported.')
@allowed([
  'dev'
  'prod'
])
param environmentName string

@description('Optional tags shared by all deployed resources.')
param tags object = {}

@description('Optional container image URI override. By default azd deploy updates the app with the image it builds and pushes to the provisioned registry.')
param containerImage string = ''

@description('Service name as defined in azure.yaml. During the NAT cutover this stays distinct from the legacy app so azd deploy targets the new Container App unambiguously.')
param serviceName string = 'web-nat'

@description('Set to true to provision the Microsoft Entra ID app registration via the Graph Bicep extension.')
param deployEntraAppRegistration bool = false

@description('Display name used for the Microsoft Entra ID app registration when deployEntraAppRegistration is true.')
param entraAppRegistrationName string = take('Fantasy Cards Generator (${toUpper(environmentName)})', 120)

@description('Description used for the Microsoft Entra ID app registration when deployEntraAppRegistration is true.')
param entraAppRegistrationDescription string = 'Multi-tenant Microsoft Entra ID app registration for the fantasy-cards-generator web app.'

@description('Local-development OIDC redirect URI to register when deployEntraAppRegistration is true.')
param entraLocalRedirectUri string = 'https://localhost:8000/auth/callback'

@description('Path appended to the deployed Container Apps URL to form the production OIDC redirect URI.')
param entraRedirectPath string = '/auth/callback'

@description('Path appended to the deployed Container Apps URL to form the production OIDC post-logout redirect URI.')
param entraPostLogoutRedirectPath string = '/'

@description('Legacy Cosmos firewall IP rule to retain temporarily during parallel cutover. Clear this after the NAT-backed environment passes smoke tests.')
param legacyCosmosIpRule string = ''

@secure()
@description('Session cookie signing secret stored in Key Vault and mirrored into the Container App secret set (APP_SESSION_SECRET_KEY). Set via `azd env set APP_SESSION_SECRET_KEY <value>` before provisioning.')
param appSessionSecretKeyValue string = ''

@secure()
@description('Microsoft Entra ID client secret stored in Key Vault and mirrored into the Container App secret set (ENTRA_CLIENT_SECRET). Populated automatically by hooks/gen_client_secret.sh when deployEntraAppRegistration=true.')
param entraClientSecretValue string = ''

@description('Cosmos DB SQL database name for application data.')
param cosmosDatabaseName string = 'appdb'

@description('Cosmos DB SQL container name for persisted card documents.')
param cosmosContainerName string = 'cards'

@description('Blob container name for persisted card assets.')
param cardAssetsContainerName string = 'card-assets'

@description('Azure AI Foundry project name for the current environment.')
param aiFoundryProjectName string = 'fantasy-cards'

@description('Azure AI Foundry project display name for the current environment.')
param aiFoundryProjectDisplayName string = 'Fantasy Cards'

@allowed([
  'ServicePrincipal'
  'User'
])
@description('Microsoft Entra principal type running the deployment. Supplied by azd through AZURE_PRINCIPAL_TYPE for ARM role assignments.')
param deployerPrincipalType string

@description('Azure AI Foundry deployment name for the text model.')
param aiFoundryTextDeploymentName string = 'gpt-5-5'

@description('Azure AI Foundry model name for text generation.')
param aiFoundryTextModelName string = 'gpt-5.5'

@description('Azure AI Foundry model version for text generation. Verify against the live catalog before deployment.')
param aiFoundryTextModelVersion string = '2026-04-24'

@description('Azure AI Foundry SKU for the text deployment. Verify against regional quota availability before deployment.')
param aiFoundryTextDeploymentSkuName string = 'GlobalStandard'

@description('Azure AI Foundry capacity units for the text deployment. Verify against live quota before deployment.')
param aiFoundryTextDeploymentCapacity int = 1

@description('Azure AI Foundry deployment name for the image model.')
param aiFoundryImageDeploymentName string = 'gpt-image-2'

@description('Azure AI Foundry model name for image generation.')
param aiFoundryImageModelName string = 'gpt-image-2'

@description('Azure AI Foundry model version for image generation. Verify against the live catalog before deployment.')
param aiFoundryImageModelVersion string = '2026-04-21'

@description('Azure AI Foundry SKU for the image deployment. Verify against regional quota availability before deployment.')
param aiFoundryImageDeploymentSkuName string = 'GlobalStandard'

@description('Azure AI Foundry capacity units for the image deployment. Verify against live quota before deployment.')
param aiFoundryImageDeploymentCapacity int = 1

@description('AI orchestration mode for the app runtime.')
param aiMode string = 'live'

@description('Persistence mode for the app runtime.')
param persistenceMode string = 'azure'

@description('Moderation service for the app runtime.')
param moderationService string = 'heuristic'

@description('Moderation policy name for the app runtime.')
param moderationPolicyName string = 'conservative-v1'

@description('Per-user request limit for generation endpoints.')
param rateLimitUserRequests int = 6

@description('Per-user rate-limit window in seconds.')
param rateLimitUserWindowSeconds int = 60

@description('Per-IP request limit for generation endpoints.')
param rateLimitIpRequests int = 12

@description('Per-IP rate-limit window in seconds.')
param rateLimitIpWindowSeconds int = 60

@description('Trusted reverse-proxy hops for request IP extraction. Keep 0 for direct app access; use 1 behind Azure Container Apps ingress so only the ACA rightmost appended X-Forwarded-For hop is trusted.')
param trustedProxyHops int = 1

@description('Maximum retries for retryable upstream dependencies.')
param upstreamMaxRetries int = 2

@description('Maximum image retries. Keep zero because an image request may complete after the client times out.')
param imageMaxRetries int = 0

@description('Base backoff in seconds between retry attempts.')
param upstreamBaseBackoffSeconds string = '0.15'

@description('Text generation timeout in seconds.')
param textTimeoutSeconds string = '20'

@description('Image generation timeout in seconds.')
param imageTimeoutSeconds string = '150'

@description('Overall request timeout in seconds.')
param overallTimeoutSeconds string = '225'

@description('Sanitized audit retention in days.')
param auditRetentionDays int = 30

@description('Requested image size for artwork generation.')
param imageSize string = '1024x1024'

var namePrefix = 'fcg'
var deployerPrincipalId = deployer().objectId
var resourceToken = toLower('${namePrefix}-${environmentName}')
var uniqueToken = uniqueString(subscription().id, resourceGroup().id)
var logAnalyticsName = take('${resourceToken}-law', 63)
var appInsightsName = take('${resourceToken}-appi', 63)
var keyVaultBase = replace(
  'kv${namePrefix}${environmentName}${uniqueToken}',
  '-',
  ''
)
var keyVaultName = toLower(take(keyVaultBase, 24))
var containerAppsEnvironmentName = take('${resourceToken}-cae-nat', 32)
var containerAppName = take('${resourceToken}-app-nat', 32)
var cosmosAccountName = toLower(take('${resourceToken}-cosmos-${uniqueToken}', 44))
var storageAccountBase = replace('st${namePrefix}${environmentName}${uniqueToken}', '-', '')
var storageAccountName = toLower(take(storageAccountBase, 24))
var aiFoundryAccountBase = replace('ai${namePrefix}${environmentName}${uniqueToken}', '-', '')
var aiFoundryAccountName = toLower(take(aiFoundryAccountBase, 24))
var registryName = toLower(take('${namePrefix}${environmentName}${uniqueToken}acr', 50))
var acrPullIdentityName = take('${resourceToken}-acr-pull', 128)
var virtualNetworkName = take('${resourceToken}-vnet', 64)
var containerAppsSubnetName = 'aca-infra'
var privateEndpointSubnetName = 'private-endpoints'
var natGatewayName = take('${resourceToken}-nat', 64)
var natGatewayPublicIpName = take('${resourceToken}-nat-pip', 64)

module monitoring './modules/monitoring.bicep' = {
  name: 'monitoring'
  params: {
    location: location
    logAnalyticsWorkspaceName: logAnalyticsName
    appInsightsName: appInsightsName
    tags: tags
  }
}

module registry './modules/container-registry.bicep' = {
  name: 'container-registry'
  params: {
    acrPullIdentityName: acrPullIdentityName
    location: location
    registryName: registryName
    tags: tags
  }
}

module security './modules/security.bicep' = {
  name: 'security'
  params: {
    appSessionSecretKeyValue: appSessionSecretKeyValue
    entraClientSecretValue: entraClientSecretValue
    keyVaultName: keyVaultName
    keyVaultAccessPrincipalId: registry.outputs.acrPullIdentityPrincipalId
    location: location
    tags: tags
  }
}

module network './modules/network.bicep' = {
  name: 'network'
  params: {
    containerAppsSubnetName: containerAppsSubnetName
    privateEndpointSubnetName: privateEndpointSubnetName
    location: location
    natGatewayName: natGatewayName
    natGatewayPublicIpName: natGatewayPublicIpName
    tags: tags
    virtualNetworkName: virtualNetworkName
  }
}

module containerAppsEnvironment './modules/container-apps-environment.bicep' = {
  name: 'container-apps-environment'
  params: {
    containerAppsEnvironmentName: containerAppsEnvironmentName
    infrastructureSubnetId: network.outputs.containerAppsSubnetResourceId
    location: location
    logAnalyticsWorkspaceCustomerId: monitoring.outputs.logAnalyticsWorkspaceCustomerId
    logAnalyticsWorkspaceSharedKey: monitoring.outputs.logAnalyticsWorkspaceSharedKey
    tags: tags
  }
}

var predictedContainerAppUrl = 'https://${containerAppName}.${containerAppsEnvironment.outputs.containerAppsEnvironmentDefaultDomain}'
var deployedAuthRedirectUri = '${predictedContainerAppUrl}${entraRedirectPath}'
var deployedPostLogoutRedirectUri = '${predictedContainerAppUrl}${entraPostLogoutRedirectPath}'

module appRegistration './modules/app-registration.bicep' = if (deployEntraAppRegistration) {
  name: 'app-registration'
  params: {
    appDescription: entraAppRegistrationDescription
    appName: entraAppRegistrationName
    appUniqueName: take('fantasy-cards-generator-${environmentName}', 64)
    redirectUris: [
      entraLocalRedirectUri
      deployedAuthRedirectUri
    ]
  }
}

var entraClientId = deployEntraAppRegistration ? appRegistration!.outputs.appId : ''

module containerApps './modules/container-apps.bicep' = {
  name: 'container-apps'
  params: {
    acrLoginServer: registry.outputs.registryLoginServer
    acrPullIdentityResourceId: registry.outputs.acrPullIdentityResourceId
    aiMode: aiMode
    appInsightsConnectionString: monitoring.outputs.appInsightsConnectionString
    appSessionSecretKeyValue: appSessionSecretKeyValue
    auditRetentionDays: auditRetentionDays
    blobContainerName: cardAssetsContainerName
    blobEndpoint: 'https://${storageAccountName}.blob.${environment().suffixes.storage}/'
    containerAppName: containerAppName
    containerAppsEnvironmentResourceId: containerAppsEnvironment.outputs.containerAppsEnvironmentResourceId
    containerImage: empty(containerImage) ? '${registry.outputs.registryLoginServer}/fantasy-cards-generator:latest' : containerImage
    cosmosContainerName: cosmosContainerName
    cosmosDatabaseName: cosmosDatabaseName
    cosmosEndpoint: 'https://${cosmosAccountName}.documents.azure.com:443/'
    entraClientId: entraClientId
    entraClientSecretValue: entraClientSecretValue
    entraPostLogoutRedirectUri: deployEntraAppRegistration ? deployedPostLogoutRedirectUri : ''
    entraRedirectUri: deployEntraAppRegistration ? deployedAuthRedirectUri : ''
    foundryApiVersion: '2025-03-01-preview'
    foundryEndpoint: 'https://${aiFoundryAccountName}.cognitiveservices.azure.com/'
    foundryImageDeployment: aiFoundryImageDeploymentName
    foundryTextDeployment: aiFoundryTextDeploymentName
    imageMaxRetries: imageMaxRetries
    imageTimeoutSeconds: imageTimeoutSeconds
    imageSize: imageSize
    keyVaultUri: security.outputs.keyVaultUri
    location: location
    moderationPolicyName: moderationPolicyName
    moderationService: moderationService
    overallTimeoutSeconds: overallTimeoutSeconds
    persistenceMode: persistenceMode
    rateLimitIpRequests: rateLimitIpRequests
    rateLimitIpWindowSeconds: rateLimitIpWindowSeconds
    rateLimitUserRequests: rateLimitUserRequests
    rateLimitUserWindowSeconds: rateLimitUserWindowSeconds
    serviceName: serviceName
    tags: tags
    trustedProxyHops: trustedProxyHops
    upstreamBaseBackoffSeconds: upstreamBaseBackoffSeconds
    upstreamMaxRetries: upstreamMaxRetries
    textTimeoutSeconds: textTimeoutSeconds
  }
}

module cosmosDb './modules/cosmos-db.bicep' = {
  name: 'cosmos-db'
  params: {
    accountName: cosmosAccountName
    containerAppPrincipalId: containerApps.outputs.containerAppPrincipalId
    containerName: cosmosContainerName
    databaseName: cosmosDatabaseName
    deployerPrincipalId: deployerPrincipalId
    legacyIpRule: legacyCosmosIpRule
    location: location
    natGatewayPublicIpAddress: network.outputs.natGatewayPublicIpAddress
    tags: tags
  }
}

module storage './modules/storage.bicep' = {
  name: 'storage'
  params: {
    containerAppPrincipalId: containerApps.outputs.containerAppPrincipalId
    containerName: cardAssetsContainerName
    deployerPrincipalId: deployerPrincipalId
    deployerPrincipalType: deployerPrincipalType
    location: location
    privateEndpointSubnetResourceId: network.outputs.privateEndpointSubnetResourceId
    storageAccountName: storageAccountName
    tags: tags
    virtualNetworkResourceId: network.outputs.virtualNetworkResourceId
  }
}

module aiFoundry './modules/ai-foundry.bicep' = {
  name: 'ai-foundry'
  params: {
    accountName: aiFoundryAccountName
    containerAppPrincipalId: containerApps.outputs.containerAppPrincipalId
    customSubDomainName: aiFoundryAccountName
    deployerPrincipalId: deployerPrincipalId
    deployerPrincipalType: deployerPrincipalType
    imageDeploymentCapacity: aiFoundryImageDeploymentCapacity
    imageDeploymentName: aiFoundryImageDeploymentName
    imageDeploymentSkuName: aiFoundryImageDeploymentSkuName
    imageModelName: aiFoundryImageModelName
    imageModelVersion: aiFoundryImageModelVersion
    location: location
    projectDisplayName: '${aiFoundryProjectDisplayName} (${toUpper(environmentName)})'
    projectName: take('${aiFoundryProjectName}-${environmentName}', 64)
    tags: tags
    textDeploymentCapacity: aiFoundryTextDeploymentCapacity
    textDeploymentName: aiFoundryTextDeploymentName
    textDeploymentSkuName: aiFoundryTextDeploymentSkuName
    textModelName: aiFoundryTextModelName
    textModelVersion: aiFoundryTextModelVersion
  }
}

output applicationInsightsName string = monitoring.outputs.appInsightsName
output aiFoundryAccountEndpoint string = aiFoundry.outputs.aiFoundryAccountEndpoint
output aiFoundryAccountName string = aiFoundry.outputs.aiFoundryAccountName
output aiFoundryAccountResourceId string = aiFoundry.outputs.aiFoundryAccountResourceId
output aiFoundryImageDeploymentName string = aiFoundry.outputs.aiFoundryImageDeploymentName
output aiFoundryProjectName string = aiFoundry.outputs.aiFoundryProjectName
output aiFoundryProjectResourceId string = aiFoundry.outputs.aiFoundryProjectResourceId
output aiFoundryTextDeploymentName string = aiFoundry.outputs.aiFoundryTextDeploymentName
output deployedAuthRedirectUri string = '${containerApps.outputs.containerAppUrl}${entraRedirectPath}'
output containerAppName string = containerApps.outputs.containerAppName
output containerAppFqdn string = containerApps.outputs.containerAppFqdn
output containerAppPrincipalId string = containerApps.outputs.containerAppPrincipalId
output containerAppResourceId string = containerApps.outputs.containerAppResourceId
output containerAppUrl string = containerApps.outputs.containerAppUrl
output containerAppsEnvironmentName string = containerAppsEnvironment.outputs.containerAppsEnvironmentName
output containerAppsEnvironmentResourceId string = containerAppsEnvironment.outputs.containerAppsEnvironmentResourceId
output cosmosAccountEndpoint string = cosmosDb.outputs.cosmosAccountEndpoint
output cosmosAccountName string = cosmosDb.outputs.cosmosAccountName
output cosmosAccountResourceId string = cosmosDb.outputs.cosmosAccountResourceId
output cosmosContainerName string = cosmosDb.outputs.cosmosContainerName
output cosmosDatabaseName string = cosmosDb.outputs.cosmosDatabaseName
output cosmosIpRules array = cosmosDb.outputs.cosmosIpRules
output keyVaultName string = security.outputs.keyVaultName
output keyVaultUri string = security.outputs.keyVaultUri
output logAnalyticsWorkspaceName string = monitoring.outputs.logAnalyticsWorkspaceName
output logAnalyticsWorkspaceResourceId string = monitoring.outputs.logAnalyticsWorkspaceResourceId
output natGatewayName string = network.outputs.natGatewayName
output natGatewayPublicIpAddress string = network.outputs.natGatewayPublicIpAddress
output natGatewayPublicIpName string = network.outputs.natGatewayPublicIpName
output storageAccountName string = storage.outputs.storageAccountName
output storageAccountResourceId string = storage.outputs.storageAccountResourceId
output storageBlobEndpoint string = storage.outputs.storageBlobEndpoint
output storageContainerName string = storage.outputs.storageContainerName
output virtualNetworkName string = network.outputs.virtualNetworkName
output containerAppsSubnetName string = network.outputs.containerAppsSubnetName
output privateEndpointSubnetName string = network.outputs.privateEndpointSubnetName
output entraClientId string = entraClientId
output entraAppObjectId string = deployEntraAppRegistration ? appRegistration!.outputs.appObjectId : ''
output entraServicePrincipalId string = deployEntraAppRegistration ? appRegistration!.outputs.servicePrincipalId : ''
output entraTenantId string = deployEntraAppRegistration ? appRegistration!.outputs.tenantId : tenant().tenantId
output entraLocalRedirectUri string = entraLocalRedirectUri
output AZURE_CONTAINER_APP_NAME string = containerApps.outputs.containerAppName
output AZURE_CONTAINER_APPS_ENVIRONMENT_NAME string = containerAppsEnvironment.outputs.containerAppsEnvironmentName
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = registry.outputs.registryLoginServer
output AZURE_CONTAINER_REGISTRY_NAME string = registry.outputs.registryName
output ENTRA_CLIENT_ID string = entraClientId
