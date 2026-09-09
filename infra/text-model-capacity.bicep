targetScope = 'resourceGroup'

@allowed([
  'dev'
])
@description('Safety gate: this targeted capacity repair is dev-only.')
param environmentName string

@description('Existing Azure AI Foundry account name.')
param accountName string

@description('Existing text model deployment name.')
param deploymentName string = 'gpt-5-5'

@description('Existing text model name.')
param modelName string = 'gpt-5.5'

@description('Existing text model version.')
param modelVersion string = '2026-04-24'

@description('Existing deployment SKU.')
param skuName string = 'GlobalStandard'

@minValue(1)
@description('Desired capacity units. Ten units provide 10 RPM and 10K TPM for the three-stage dev orchestrator.')
param capacity int = 10

@description('Existing Responsible AI policy.')
param raiPolicyName string = 'Microsoft.DefaultV2'

@allowed([
  'NoAutoUpgrade'
  'OnceCurrentVersionExpired'
  'OnceNewDefaultVersionAvailable'
])
@description('Existing model version upgrade policy.')
param versionUpgradeOption string = 'OnceNewDefaultVersionAvailable'

resource foundryAccount 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: accountName
}

resource textModelDeployment 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: foundryAccount
  name: deploymentName
  sku: {
    capacity: capacity
    name: skuName
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: modelName
      version: modelVersion
    }
    raiPolicyName: raiPolicyName
    versionUpgradeOption: versionUpgradeOption
  }
}

output deploymentResourceId string = textModelDeployment.id
output desiredCapacity int = capacity
output targetEnvironment string = environmentName
