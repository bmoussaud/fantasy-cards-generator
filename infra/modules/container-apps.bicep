@description('Deployment location for Container Apps resources.')
param location string

@description('Container Apps environment name.')
param containerAppsEnvironmentName string

@description('Container App name.')
param containerAppName string

@description('Container image used by the initial scaffold app.')
param containerImage string

@description('Log Analytics workspace customer ID for Container Apps logging.')
param logAnalyticsWorkspaceCustomerId string

@secure()
@description('Log Analytics workspace shared key for Container Apps logging.')
param logAnalyticsWorkspaceSharedKey string

@secure()
@description('Application Insights connection string used by the app foundation.')
param appInsightsConnectionString string

@description('Key Vault URI injected in the scaffold app environment.')
param keyVaultUri string

@description('Optional tags shared by Container Apps resources.')
param tags object = {}

resource containerAppsEnvironment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: containerAppsEnvironmentName
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalyticsWorkspaceCustomerId
        sharedKey: logAnalyticsWorkspaceSharedKey
      }
    }
    zoneRedundant: false
  }
}

resource containerApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: containerAppName
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned'
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
      secrets: [
        {
          name: 'applicationinsights-connection-string'
          value: appInsightsConnectionString
        }
      ]
    }
    managedEnvironmentId: containerAppsEnvironment.id
    template: {
      containers: [
        {
          name: 'web'
          image: containerImage
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: [
            {
              name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
              secretRef: 'applicationinsights-connection-string'
            }
            {
              name: 'KEY_VAULT_URI'
              value: keyVaultUri
            }
          ]
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
output containerAppPrincipalId string = containerApp.identity.principalId
output containerAppResourceId string = containerApp.id
output containerAppsEnvironmentName string = containerAppsEnvironment.name
output containerAppsEnvironmentResourceId string = containerAppsEnvironment.id
