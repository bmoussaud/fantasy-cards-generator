@description('Deployment location for Azure AI Foundry resources.')
param location string

@description('Azure AI Foundry account name. Must be globally unique.')
param accountName string

@description('Custom subdomain name for the Azure AI Foundry account. Must be globally unique.')
param customSubDomainName string

@description('Azure AI Foundry project name.')
param projectName string

@description('Azure AI Foundry project display name.')
param projectDisplayName string

@description('Managed identity principal ID for the Container App that needs Azure AI Foundry access.')
param containerAppPrincipalId string

@description('Deployment name for the text model used by the backend.')
param textDeploymentName string

@description('Text model name to deploy. Verify availability in the target region and subscription before rollout.')
param textModelName string

@description('Text model version to deploy. This is a placeholder default until real Foundry quotas/catalog availability are confirmed.')
param textModelVersion string = '1'

@description('SKU name for the text deployment. This is parameterized because exact regional support may vary.')
param textDeploymentSkuName string = 'GlobalStandard'

@description('Capacity units for the text deployment. Confirm against real quota availability before production rollout.')
param textDeploymentCapacity int = 1

@description('Deployment name for the image model used by the backend.')
param imageDeploymentName string

@description('Image model name to deploy. Verify availability in the target region and subscription before rollout.')
param imageModelName string

@description('Image model version to deploy. This is a placeholder default until real Foundry quotas/catalog availability are confirmed.')
param imageModelVersion string = '1'

@description('SKU name for the image deployment. This is parameterized because exact regional support may vary.')
param imageDeploymentSkuName string = 'GlobalStandard'

@description('Capacity units for the image deployment. Confirm against real quota availability before production rollout.')
param imageDeploymentCapacity int = 1

@description('Optional tags shared by Azure AI Foundry resources.')
param tags object = {}

var cognitiveServicesUserRoleDefinitionId = 'a97b65f3-24c7-4388-baec-2e87135dc908'

resource foundryAccount 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: accountName
  location: location
  tags: tags
  kind: 'AIServices'
  identity: {
    type: 'SystemAssigned'
  }
  sku: {
    name: 'S0'
  }
  properties: {
    allowProjectManagement: true
    customSubDomainName: customSubDomainName
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
    restrictOutboundNetworkAccess: false
  }
}

resource textModelDeployment 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: foundryAccount
  name: textDeploymentName
  dependsOn: [
    aiFoundryProject
  ]
  sku: {
    capacity: textDeploymentCapacity
    name: textDeploymentSkuName
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: textModelName
      version: textModelVersion
    }
  }
}

resource imageModelDeployment 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: foundryAccount
  name: imageDeploymentName
  dependsOn: [
    textModelDeployment
  ]
  sku: {
    capacity: imageDeploymentCapacity
    name: imageDeploymentSkuName
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: imageModelName
      version: imageModelVersion
    }
  }
}

resource aiFoundryProject 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' = {
  name: projectName
  location: location
  parent: foundryAccount
  properties: {
    description: 'Azure AI Foundry project for the Fantasy Cards Generator ${projectName} environment.'
    displayName: projectDisplayName
  }
}

resource cognitiveServicesUserRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: foundryAccount
  name: guid(foundryAccount.id, containerAppPrincipalId, cognitiveServicesUserRoleDefinitionId)
  properties: {
    principalId: containerAppPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cognitiveServicesUserRoleDefinitionId)
  }
}

output aiFoundryAccountName string = foundryAccount.name
output aiFoundryAccountResourceId string = foundryAccount.id
output aiFoundryAccountEndpoint string = foundryAccount.properties.endpoint
output aiFoundryProjectName string = aiFoundryProject.name
output aiFoundryProjectResourceId string = aiFoundryProject.id
output aiFoundryTextDeploymentName string = textDeploymentName
output aiFoundryImageDeploymentName string = imageDeploymentName
