@description('Deployment location for Container Apps resources.')
param location string

@description('Resource ID of the Container Apps managed environment the app runs in.')
param containerAppsEnvironmentResourceId string

@description('Container App name.')
param containerAppName string

@description('Container image used by the web app.')
param containerImage string

@description('Azure Container Registry login server used by the web app image.')
param acrLoginServer string

@description('Managed identity resource ID used by Container Apps to pull from the registry.')
param acrPullIdentityResourceId string

@description('Service name as defined in azure.yaml.')
param serviceName string = 'web'

@secure()
@description('Application Insights connection string used by the app foundation.')
param appInsightsConnectionString string

@description('Key Vault URI injected in the web app environment.')
param keyVaultUri string

@secure()
@description('Session cookie signing secret injected into the Container App. Empty skips creating the secret.')
param appSessionSecretKeyValue string = ''

@secure()
@description('Microsoft Entra ID client secret injected into the Container App. Empty skips creating the secret.')
param entraClientSecretValue string = ''

@description('Microsoft Entra ID application (client) ID injected as ENTRA_CLIENT_ID. Empty when Entra app registration is disabled.')
param entraClientId string = ''

@description('Deployed OIDC redirect URI injected as ENTRA_REDIRECT_URI. Empty when Entra app registration is disabled.')
param entraRedirectUri string = ''

@description('Deployed post-logout redirect URI injected as ENTRA_POST_LOGOUT_REDIRECT_URI. Empty when Entra app registration is disabled.')
param entraPostLogoutRedirectUri string = ''

@description('AI orchestration mode injected as AI_MODE.')
param aiMode string = 'live'

@description('Persistence mode injected as PERSISTENCE_MODE.')
param persistenceMode string = 'azure'

@description('Azure AI Foundry endpoint injected as FOUNDRY_ENDPOINT.')
param foundryEndpoint string = ''

@description('Azure AI Foundry API version injected as FOUNDRY_API_VERSION.')
param foundryApiVersion string = '2025-03-01-preview'

@description('Azure AI Foundry text deployment injected as FOUNDRY_TEXT_DEPLOYMENT.')
param foundryTextDeployment string = 'gpt-5-5'

@description('Azure AI Foundry image deployment injected as FOUNDRY_IMAGE_DEPLOYMENT.')
param foundryImageDeployment string = 'gpt-image-2'

@description('Cosmos DB endpoint injected as COSMOS_ENDPOINT.')
param cosmosEndpoint string = ''

@description('Cosmos DB database name injected as COSMOS_DATABASE_NAME.')
param cosmosDatabaseName string = 'appdb'

@description('Cosmos DB container name injected as COSMOS_CONTAINER_NAME.')
param cosmosContainerName string = 'cards'

@description('Blob endpoint injected as BLOB_ENDPOINT.')
param blobEndpoint string = ''

@description('Blob container name injected as BLOB_CONTAINER_NAME.')
param blobContainerName string = 'card-assets'

@description('Moderation service injected as MODERATION_SERVICE.')
param moderationService string = 'heuristic'

@description('Moderation policy name injected as MODERATION_POLICY_NAME.')
param moderationPolicyName string = 'conservative-v1'

@description('Per-user request limit injected as RATE_LIMIT_USER_REQUESTS.')
param rateLimitUserRequests int = 6

@description('Per-user rate-limit window injected as RATE_LIMIT_USER_WINDOW_SECONDS.')
param rateLimitUserWindowSeconds int = 60

@description('Per-IP request limit injected as RATE_LIMIT_IP_REQUESTS.')
param rateLimitIpRequests int = 12

@description('Per-IP rate-limit window injected as RATE_LIMIT_IP_WINDOW_SECONDS.')
param rateLimitIpWindowSeconds int = 60

@description('Maximum upstream retries injected as UPSTREAM_MAX_RETRIES.')
param upstreamMaxRetries int = 2

@description('Base backoff seconds injected as UPSTREAM_BASE_BACKOFF_SECONDS.')
param upstreamBaseBackoffSeconds string = '0.15'

@description('Per-upstream timeout injected as UPSTREAM_TIMEOUT_SECONDS.')
param upstreamTimeoutSeconds string = '8'

@description('Overall request timeout injected as OVERALL_TIMEOUT_SECONDS.')
param overallTimeoutSeconds string = '18'

@description('Sanitized audit retention in days injected as AUDIT_RETENTION_DAYS.')
param auditRetentionDays int = 30

@description('Requested image size injected as IMAGE_SIZE.')
param imageSize string = '1024x1024'

@description('Optional tags shared by Container Apps resources.')
param tags object = {}

var containerAppSecrets = concat(
  [
    {
      name: 'applicationinsights-connection-string'
      value: appInsightsConnectionString
    }
  ],
  !empty(appSessionSecretKeyValue)
    ? [
        {
          name: 'app-session-secret-key'
          value: appSessionSecretKeyValue
        }
      ]
    : [],
  !empty(entraClientSecretValue)
    ? [
        {
          name: 'entra-client-secret'
          value: entraClientSecretValue
        }
      ]
    : []
)

var containerAppEnv = concat(
  [
    {
      name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
      secretRef: 'applicationinsights-connection-string'
    }
    {
      name: 'AI_MODE'
      value: aiMode
    }
    {
      name: 'PERSISTENCE_MODE'
      value: persistenceMode
    }
    {
      name: 'FOUNDRY_ENDPOINT'
      value: foundryEndpoint
    }
    {
      name: 'FOUNDRY_API_VERSION'
      value: foundryApiVersion
    }
    {
      name: 'FOUNDRY_TEXT_DEPLOYMENT'
      value: foundryTextDeployment
    }
    {
      name: 'FOUNDRY_IMAGE_DEPLOYMENT'
      value: foundryImageDeployment
    }
    {
      name: 'COSMOS_ENDPOINT'
      value: cosmosEndpoint
    }
    {
      name: 'COSMOS_DATABASE_NAME'
      value: cosmosDatabaseName
    }
    {
      name: 'COSMOS_CONTAINER_NAME'
      value: cosmosContainerName
    }
    {
      name: 'BLOB_ENDPOINT'
      value: blobEndpoint
    }
    {
      name: 'BLOB_CONTAINER_NAME'
      value: blobContainerName
    }
    {
      name: 'MODERATION_SERVICE'
      value: moderationService
    }
    {
      name: 'MODERATION_POLICY_NAME'
      value: moderationPolicyName
    }
    {
      name: 'RATE_LIMIT_USER_REQUESTS'
      value: string(rateLimitUserRequests)
    }
    {
      name: 'RATE_LIMIT_USER_WINDOW_SECONDS'
      value: string(rateLimitUserWindowSeconds)
    }
    {
      name: 'RATE_LIMIT_IP_REQUESTS'
      value: string(rateLimitIpRequests)
    }
    {
      name: 'RATE_LIMIT_IP_WINDOW_SECONDS'
      value: string(rateLimitIpWindowSeconds)
    }
    {
      name: 'UPSTREAM_MAX_RETRIES'
      value: string(upstreamMaxRetries)
    }
    {
      name: 'UPSTREAM_BASE_BACKOFF_SECONDS'
      value: upstreamBaseBackoffSeconds
    }
    {
      name: 'UPSTREAM_TIMEOUT_SECONDS'
      value: upstreamTimeoutSeconds
    }
    {
      name: 'OVERALL_TIMEOUT_SECONDS'
      value: overallTimeoutSeconds
    }
    {
      name: 'AUDIT_RETENTION_DAYS'
      value: string(auditRetentionDays)
    }
    {
      name: 'IMAGE_SIZE'
      value: imageSize
    }
    {
      name: 'KEY_VAULT_URI'
      value: keyVaultUri
    }
  ],
  !empty(appSessionSecretKeyValue)
    ? [
        {
          name: 'APP_SESSION_SECRET_KEY'
          secretRef: 'app-session-secret-key'
        }
      ]
    : [],
  !empty(entraClientId)
    ? [
        {
          name: 'ENTRA_CLIENT_ID'
          value: entraClientId
        }
      ]
    : [],
  !empty(entraClientSecretValue)
    ? [
        {
          name: 'ENTRA_CLIENT_SECRET'
          secretRef: 'entra-client-secret'
        }
      ]
    : [],
  !empty(entraRedirectUri)
    ? [
        {
          name: 'ENTRA_REDIRECT_URI'
          value: entraRedirectUri
        }
      ]
    : [],
  !empty(entraPostLogoutRedirectUri)
    ? [
        {
          name: 'ENTRA_POST_LOGOUT_REDIRECT_URI'
          value: entraPostLogoutRedirectUri
        }
      ]
    : []
)

resource containerApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: containerAppName
  location: location
  tags: union(tags, {
    'azd-service-name': serviceName
  })
  identity: {
    type: 'SystemAssigned,UserAssigned'
    userAssignedIdentities: {
      '${acrPullIdentityResourceId}': {}
    }
  }
  properties: {
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        allowInsecure: false
        external: true
        targetPort: 8000
        transport: 'auto'
      }
      secrets: containerAppSecrets
      registries: [
        {
          server: acrLoginServer
          identity: acrPullIdentityResourceId
        }
      ]
    }
    managedEnvironmentId: containerAppsEnvironmentResourceId
    template: {
      containers: [
        {
          name: 'web'
          image: containerImage
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: containerAppEnv
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 2
        rules: []
      }
    }
  }
}

output containerAppName string = containerApp.name
output containerAppFqdn string = containerApp.properties.configuration.ingress.fqdn
output containerAppPrincipalId string = containerApp.identity.principalId
output containerAppResourceId string = containerApp.id
output containerAppUrl string = 'https://${containerApp.properties.configuration.ingress.fqdn}'
