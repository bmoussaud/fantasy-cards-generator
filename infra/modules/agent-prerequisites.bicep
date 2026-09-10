@description('Azure Container Registry name for hosted-agent image pulls.')
@minLength(5)
param registryName string

@description('Azure AI Foundry account name.')
@minLength(2)
param accountName string

@description('Azure AI Foundry project name.')
@minLength(2)
param projectName string

@description('Foundry project system-assigned managed-identity principal ID.')
@minLength(1)
param projectPrincipalId string

@description('Create the Foundry project → ACR registry connection. Only after confirming no existing registry connection.')
param createRegistryConnection bool = false

@description('Name discovered from the project connection inventory; never rename an existing connection.')
param registryConnectionName string = ''

var acrPullRoleDefinitionId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '7f951dda-4ed3-4680-a7ca-43fe172d538d'
)

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: registryName
}

resource foundryAccount 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: accountName
}

resource aiFoundryProject 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' existing = {
  parent: foundryAccount
  name: projectName
}

// ACR Pull for the Foundry project managed identity so the hosted agent
// image can be pulled during agent deployment.  Uses the same canonical
// role ID as the card-orchestrator prerequisites repair facade.
resource projectRegistryPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: registry
  name: guid(registry.id, projectPrincipalId, acrPullRoleDefinitionId)
  properties: {
    principalId: projectPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: acrPullRoleDefinitionId
  }
}

// azd pins connections to this preview: GA 2025-06-01 has a known
// projects/connections resolution failure (MissingApiVersionParameter).
resource connectionAccount 'Microsoft.CognitiveServices/accounts@2025-04-01-preview' existing = {
  name: accountName

  resource project 'projects' existing = {
    name: projectName
  }
}

resource registryConnection 'Microsoft.CognitiveServices/accounts/projects/connections@2025-04-01-preview' = if (createRegistryConnection) {
  parent: connectionAccount::project
  name: empty(registryConnectionName) ? '${registryName}-conn' : registryConnectionName
  properties: {
    category: 'ContainerRegistry'
    target: registry.properties.loginServer
    authType: 'ManagedIdentity'
    credentials: {
      clientId: aiFoundryProject.identity.principalId
      resourceId: registry.id
    }
    isSharedToAll: true
    metadata: {
      ResourceId: registry.id
    }
  }
  dependsOn: [
    projectRegistryPull
  ]
}

output registryConnectionName string = createRegistryConnection && empty(registryConnectionName)
  ? '${registryName}-conn'
  : registryConnectionName
