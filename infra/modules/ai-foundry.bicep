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

module foundryAccount 'br/public:avm/res/cognitive-services/account:0.9.2' = {
  name: 'ai-foundry-account'
  params: {
    name: accountName
    kind: 'AIServices'
    location: location
    allowProjectManagement: true
    customSubDomainName: customSubDomainName
    deployments: [
      {
        model: {
          format: 'OpenAI'
          name: textModelName
          version: textModelVersion
        }
        name: textDeploymentName
        sku: {
          capacity: textDeploymentCapacity
          name: textDeploymentSkuName
        }
      }
      {
        model: {
          format: 'OpenAI'
          name: imageModelName
          version: imageModelVersion
        }
        name: imageDeploymentName
        sku: {
          capacity: imageDeploymentCapacity
          name: imageDeploymentSkuName
        }
      }
    ]
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
    restrictOutboundNetworkAccess: false
    roleAssignments: [
      {
        principalId: containerAppPrincipalId
        principalType: 'ServicePrincipal'
        roleDefinitionIdOrName: 'Cognitive Services User'
      }
    ]
    sku: 'S0'
    tags: tags
  }
}

resource aiFoundryAccount 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: foundryAccount.outputs.name
}

resource aiFoundryProject 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' = {
  name: projectName
  location: location
  parent: aiFoundryAccount
  properties: {
    description: 'Azure AI Foundry project for the Fantasy Cards Generator ${projectName} environment.'
    displayName: projectDisplayName
  }
}

output aiFoundryAccountName string = foundryAccount.outputs.name
output aiFoundryAccountResourceId string = foundryAccount.outputs.resourceId
output aiFoundryAccountEndpoint string = foundryAccount.outputs.endpoint
output aiFoundryProjectName string = aiFoundryProject.name
output aiFoundryProjectResourceId string = aiFoundryProject.id
output aiFoundryTextDeploymentName string = textDeploymentName
output aiFoundryImageDeploymentName string = imageDeploymentName
