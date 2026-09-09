targetScope = 'subscription'

@allowed([
  'dev'
  'prod'
])
@description('Dedicated hosted-agent environment. Production execution has an additional launcher approval gate.')
param environmentName string

@minLength(1)
param resourceGroupName string

@minLength(2)
param accountName string

@minLength(2)
param projectName string

@minLength(1)
@description('Verified existing project endpoint, not an account inference endpoint.')
param projectEndpoint string

@minLength(5)
param registryName string

@minLength(1)
param containerAppName string

@description('Explicit opt-in after reviewing what-if and existing role/connection identities.')
param enablePrerequisites bool = false

@description('Create the project connection only after confirming no existing registry connection.')
param createRegistryConnection bool = false

@description('Name discovered from the project connection inventory; never rename an existing connection.')
param registryConnectionName string = ''

@minLength(1)
@description('Existing Log Analytics workspace resource ID from root infra. Monitoring is mandatory.')
param logAnalyticsWorkspaceResourceId string

@minLength(1)
@description('Existing Application Insights resource ID linked to the Foundry project by root infra.')
param appInsightsResourceId string

@description('Enable agent monitoring alerts. Requires at least one action group receiver to take effect.')
param enableAgentAlerts bool = false

@description('Action Group email receivers for agent alerts.')
param agentAlertEmailReceivers array = []

@description('Action Group webhook receivers for agent alerts. Do not embed credentials in serviceUri.')
param agentAlertWebhookReceivers array = []

resource existingGroup 'Microsoft.Resources/resourceGroups@2024-03-01' existing = {
  name: resourceGroupName
}

resource foundryAccount 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  scope: existingGroup
  name: accountName
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' existing = {
  parent: foundryAccount
  name: projectName
}

resource containerApp 'Microsoft.App/containerApps@2025-01-01' existing = {
  scope: existingGroup
  name: containerAppName
}

module prerequisites './modules/prerequisites.bicep' = if (enablePrerequisites) {
  name: 'card-orchestrator-${environmentName}-prerequisites'
  scope: existingGroup
  params: {
    accountName: accountName
    projectName: projectName
    registryName: registryName
    containerAppPrincipalId: containerApp.identity.principalId
    projectPrincipalId: project.identity.principalId
    createRegistryConnection: createRegistryConnection
    registryConnectionName: registryConnectionName
  }
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  scope: existingGroup
  name: registryName
}

module agentMonitoring './modules/agent-monitoring.bicep' = {
  name: 'card-orchestrator-${environmentName}-agent-monitoring'
  scope: existingGroup
  params: {
    location: existingGroup.location
    environmentName: environmentName
    containerAppName: containerAppName
    appInsightsResourceId: appInsightsResourceId
    logAnalyticsWorkspaceResourceId: logAnalyticsWorkspaceResourceId
    enableAlerts: enableAgentAlerts
    actionGroupEmailReceivers: agentAlertEmailReceivers
    actionGroupWebhookReceivers: agentAlertWebhookReceivers
  }
}

output AZURE_AI_PROJECT_ID string = project.id
output AZURE_AI_PROJECT_ENDPOINT string = projectEndpoint
output FOUNDRY_PROJECT_ENDPOINT string = projectEndpoint
output AZURE_AI_ACCOUNT_NAME string = accountName
output AZURE_AI_PROJECT_NAME string = projectName
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = registry.properties.loginServer
output AZURE_CONTAINER_REGISTRY_RESOURCE_ID string = registry.id
output AZURE_AI_PROJECT_ACR_CONNECTION_NAME string = enablePrerequisites && createRegistryConnection && empty(registryConnectionName)
  ? '${registryName}-conn'
  : registryConnectionName
