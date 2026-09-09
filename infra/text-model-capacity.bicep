targetScope = 'resourceGroup'

var devSubscriptionId = 'b8ff3e15-7e2d-4fac-a773-992fb59ccedd'
var devResourceGroupName = 'rg-fcag-dev'
var devArmDeploymentName = 'dev-text-model-capacity-10'
var devFoundryAccountName = 'aifcagdevqhg3qc4rlbt4g'
var textDeploymentName = 'gpt-5-5'
var subscriptionIsExactDev = subscription().subscriptionId == devSubscriptionId
var resourceGroupIsExactDev = resourceGroup().name == devResourceGroupName
var armDeploymentNameIsExact = deployment().name == devArmDeploymentName
var targetIsExactDev = subscriptionIsExactDev && resourceGroupIsExactDev && armDeploymentNameIsExact
var validatedTarget = targetIsExactDev
  ? {
      accountName: devFoundryAccountName
      deploymentName: textDeploymentName
    }
  : fail('Refusing model-capacity update: target must be the exact approved dev subscription, resource group, and ARM deployment name.')

resource foundryAccount 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: validatedTarget.accountName
}

resource textModelDeployment 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: foundryAccount
  name: validatedTarget.deploymentName
  sku: {
    capacity: 10
    name: 'GlobalStandard'
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: 'gpt-5.5'
      version: '2026-04-24'
    }
    raiPolicyName: 'Microsoft.DefaultV2'
    versionUpgradeOption: 'OnceNewDefaultVersionAvailable'
  }
}

output deploymentResourceId string = textModelDeployment.id
output desiredCapacity int = textModelDeployment.sku.capacity
