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

@description('Managed identity resource ID used by Container Apps to pull from the registry and to read Key Vault-backed secrets.')
param acrPullIdentityResourceId string

@description('Service name as defined in azure.yaml.')
param serviceName string = 'web'

@secure()
@description('Application Insights connection string used by the app foundation.')
param appInsightsConnectionString string

@description('Key Vault URI injected in the web app environment.')
param keyVaultUri string

@description('Key Vault secret URI for the session cookie signing key (APP_SESSION_SECRET_KEY). Empty when the secret has not been provisioned.')
param appSessionSecretKeySecretUri string = ''

@description('Key Vault secret URI for the Microsoft Entra ID client secret (ENTRA_CLIENT_SECRET). Empty when Entra app registration is disabled or the secret has not been provisioned.')
param entraClientSecretSecretUri string = ''

@description('Microsoft Entra ID application (client) ID injected as ENTRA_CLIENT_ID. Empty when Entra app registration is disabled.')
param entraClientId string = ''

@description('Deployed OIDC redirect URI injected as ENTRA_REDIRECT_URI. Empty when Entra app registration is disabled.')
param entraRedirectUri string = ''

@description('Deployed post-logout redirect URI injected as ENTRA_POST_LOGOUT_REDIRECT_URI. Empty when Entra app registration is disabled.')
param entraPostLogoutRedirectUri string = ''

@description('Optional tags shared by Container Apps resources.')
param tags object = {}

var containerAppSecrets = concat(
  [
    {
      name: 'applicationinsights-connection-string'
      value: appInsightsConnectionString
    }
  ],
  !empty(appSessionSecretKeySecretUri)
    ? [
        {
          name: 'app-session-secret-key'
          keyVaultUrl: appSessionSecretKeySecretUri
          identity: acrPullIdentityResourceId
        }
      ]
    : [],
  !empty(entraClientSecretSecretUri)
    ? [
        {
          name: 'entra-client-secret'
          keyVaultUrl: entraClientSecretSecretUri
          identity: acrPullIdentityResourceId
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
      name: 'KEY_VAULT_URI'
      value: keyVaultUri
    }
  ],
  !empty(appSessionSecretKeySecretUri)
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
  !empty(entraClientSecretSecretUri)
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
