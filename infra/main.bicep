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

@description('Placeholder container image for the initial Container App scaffold.')
param containerImage string = 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'

var namePrefix = 'fcg'
var resourceToken = toLower('${namePrefix}-${environmentName}')
var logAnalyticsName = take('${resourceToken}-law', 63)
var appInsightsName = take('${resourceToken}-appi', 63)
var keyVaultBase = replace(
  'kv${namePrefix}${environmentName}${uniqueString(subscription().id, resourceGroup().id)}',
  '-',
  ''
)
var keyVaultName = toLower(take(keyVaultBase, 24))
var containerAppsEnvironmentName = take('${resourceToken}-cae', 32)
var containerAppName = take('${resourceToken}-app', 32)

module monitoring './modules/monitoring.bicep' = {
  name: 'monitoring'
  params: {
    location: location
    logAnalyticsWorkspaceName: logAnalyticsName
    appInsightsName: appInsightsName
    tags: tags
  }
}

module security './modules/security.bicep' = {
  name: 'security'
  params: {
    keyVaultName: keyVaultName
    location: location
    tags: tags
  }
}

module containerApps './modules/container-apps.bicep' = {
  name: 'container-apps'
  params: {
    appInsightsConnectionString: monitoring.outputs.appInsightsConnectionString
    containerAppName: containerAppName
    containerAppsEnvironmentName: containerAppsEnvironmentName
    containerImage: containerImage
    keyVaultUri: security.outputs.keyVaultUri
    location: location
    logAnalyticsWorkspaceResourceId: monitoring.outputs.logAnalyticsWorkspaceResourceId
    tags: tags
  }
}

output applicationInsightsName string = monitoring.outputs.appInsightsName
output containerAppName string = containerApps.outputs.containerAppName
output containerAppResourceId string = containerApps.outputs.containerAppResourceId
output containerAppsEnvironmentName string = containerApps.outputs.containerAppsEnvironmentName
output containerAppsEnvironmentResourceId string = containerApps.outputs.containerAppsEnvironmentResourceId
output keyVaultName string = security.outputs.keyVaultName
output keyVaultUri string = security.outputs.keyVaultUri
output logAnalyticsWorkspaceName string = monitoring.outputs.logAnalyticsWorkspaceName
output logAnalyticsWorkspaceResourceId string = monitoring.outputs.logAnalyticsWorkspaceResourceId
