@description('Deployment location for Container Apps resources.')
param location string

@description('Container Apps environment name.')
param containerAppsEnvironmentName string

@description('Container App name.')
param containerAppName string

@description('Container image used by the initial scaffold app.')
param containerImage string

@description('Log Analytics workspace resource ID for Container Apps logging.')
param logAnalyticsWorkspaceResourceId string

@secure()
@description('Application Insights connection string used by the app foundation.')
param appInsightsConnectionString string

@description('Key Vault URI injected in the scaffold app environment.')
param keyVaultUri string

@description('Optional tags shared by Container Apps resources.')
param tags object = {}

module containerAppsEnvironment 'br/public:avm/res/app/managed-environment:0.13.3' = {
  name: 'container-apps-environment'
  params: {
    name: containerAppsEnvironmentName
    location: location
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsWorkspaceResourceId: logAnalyticsWorkspaceResourceId
    }
    publicNetworkAccess: 'Enabled'
    zoneRedundant: false
    tags: tags
  }
}

module containerApp 'br/public:avm/res/app/container-app:0.22.1' = {
  name: 'container-app'
  params: {
    name: containerAppName
    location: location
    environmentResourceId: containerAppsEnvironment.outputs.resourceId
    ingressExternal: true
    ingressAllowInsecure: false
    ingressTargetPort: 8000
    managedIdentities: {
      systemAssigned: true
    }
    scaleSettings: {
      minReplicas: 1
      maxReplicas: 2
    }
    secrets: [
      {
        name: 'applicationinsights-connection-string'
        value: appInsightsConnectionString
      }
    ]
    containers: [
      {
        name: 'web'
        image: containerImage
        resources: {
          cpu: 0.5
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
    tags: tags
  }
}

output containerAppName string = containerApp.outputs.name
output containerAppResourceId string = containerApp.outputs.resourceId
output containerAppsEnvironmentName string = containerAppsEnvironment.outputs.name
output containerAppsEnvironmentResourceId string = containerAppsEnvironment.outputs.resourceId
