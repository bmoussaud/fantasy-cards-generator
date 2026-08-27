@description('Deployment location for Azure Container Registry resources.')
param location string

@description('Azure Container Registry name. Must be globally unique.')
param registryName string

@description('Managed identity name used by Container Apps to pull from the registry.')
param acrPullIdentityName string

@description('Optional tags shared by Azure Container Registry resources.')
param tags object = {}

var acrPullRoleDefinitionId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'

resource containerRegistry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: registryName
  location: location
  tags: tags
  sku: {
    name: 'Basic'
  }
  properties: {
    adminUserEnabled: false
    publicNetworkAccess: 'Enabled'
  }
}

resource acrPullIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: acrPullIdentityName
  location: location
  tags: tags
}

resource acrPullRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(containerRegistry.id, acrPullIdentity.id, acrPullRoleDefinitionId)
  scope: containerRegistry
  properties: {
    principalId: acrPullIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleDefinitionId)
  }
}

output acrPullIdentityResourceId string = acrPullIdentity.id
output registryLoginServer string = containerRegistry.properties.loginServer
output registryName string = containerRegistry.name
output registryResourceId string = containerRegistry.id
