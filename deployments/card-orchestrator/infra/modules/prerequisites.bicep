targetScope = 'resourceGroup'

param accountName string
param projectName string
param registryName string
param containerAppPrincipalId string
param projectPrincipalId string
param createRegistryConnection bool = false
param registryConnectionName string = ''

var foundryUserRoleDefinitionId = '53ca6127-db72-4b80-b1b0-d745d6d5456d'
var foundryAgentConsumerRoleDefinitionId = 'eed3b665-ab3a-47b6-8f48-c9382fb1dad6'
var acrPullRoleDefinitionId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')

resource foundryAccount 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: accountName
}

resource aiFoundryProject 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' existing = {
  parent: foundryAccount
  name: projectName
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: registryName
}

// Repair facade for main's canonical assignments: exactly the same ARM IDs,
// scopes and principals, not a second set of assignments or new ownership.
resource projectManagedIdentityFoundryUserRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: foundryAccount
  name: guid(foundryAccount.id, aiFoundryProject.id, foundryUserRoleDefinitionId)
  properties: {
    principalId: aiFoundryProject.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', foundryUserRoleDefinitionId)
  }
}

resource containerAppFoundryAgentConsumerRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: aiFoundryProject
  name: guid(aiFoundryProject.id, containerAppPrincipalId, foundryAgentConsumerRoleDefinitionId)
  properties: {
    principalId: containerAppPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', foundryAgentConsumerRoleDefinitionId)
  }
}

// Matches azd's existing-project AcrPull GUID inputs, including full role ID.
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
