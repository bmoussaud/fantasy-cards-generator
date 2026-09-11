@allowed(['dev'])
param environmentName string

@minValue(1)
@description('Fail closed when the canonical module is evaluated outside the reviewed dev scope.')
#disable-next-line BCP329 // Intentional ARM parameter validation failure outside the approved scope.
param devScopeGuard int = resourceGroup().name == 'rg-fcag-dev' && subscription().subscriptionId == 'b8ff3e15-7e2d-4fac-a773-992fb59ccedd' ? 1 : 0

// Reviewed dev image already contains /app/.venv and the metadata SDK.
// Embedding the fixed script avoids a new image build or application changes.
var image = 'fcagdevqhg3qc4rlbt4gacr.azurecr.io/fantasy-cards-generator/web-nat-dev@sha256:53c95a2d0457516d715df8e2e78d996afde9124016d2f5b6381bbf0f07f7dfeb'
var jobName = 'fcag-${environmentName}-metadata-runner'
var acrPullRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')

resource environment 'Microsoft.App/managedEnvironments@2024-03-01' existing = {
  name: 'fcag-dev-cae'
}

resource vault 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: 'kvfcagdevqhg3qc4rlbt4g'
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: 'fcagdevqhg3qc4rlbt4gacr'
}

resource namedSecrets 'Microsoft.KeyVault/vaults/secrets@2023-07-01' existing = [for name in [
  'app-session-secret-key'
  'entra-client-secret'
]: {
  parent: vault
  name: name
}]

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${jobName}-id'
  location: 'eastus2'
}

resource metadataRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(resourceGroup().id, 'private-secret-metadata-v${devScopeGuard}')
  properties: {
    roleName: '${resourceGroup().name} private secret metadata'
    description: 'Only list versions and read secret metadata; no secret values or writes.'
    type: 'CustomRole'
    assignableScopes: [resourceGroup().id]
    permissions: [{
      actions: []
      notActions: []
      dataActions: ['Microsoft.KeyVault/vaults/secrets/readMetadata/action']
      notDataActions: []
    }]
  }
}

resource metadataAssignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for (name, i) in [
  'app-session-secret-key'
  'entra-client-secret'
]: {
  name: guid(vault.id, name, identity.id, metadataRole.id)
  scope: namedSecrets[i]
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: metadataRole.id
  }
}]

resource registryPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, identity.id, acrPullRoleId)
  scope: registry
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: acrPullRoleId
  }
}

// AVM 0.7.2 skips this nested job in what-if when its MI clientId is unresolved.
// A native resource keeps the complete six-resource additive preview visible.
resource job 'Microsoft.App/jobs@2024-03-01' = {
  name: jobName
  location: 'eastus2'
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identity.id}': {}
    }
  }
  properties: {
    environmentId: environment.id
    workloadProfileName: 'Consumption'
    configuration: {
      triggerType: 'Manual'
      manualTriggerConfig: {
        parallelism: 1
        replicaCompletionCount: 1
      }
      replicaRetryLimit: 0
      replicaTimeout: 120
      registries: [{
        server: registry.properties.loginServer
        identity: identity.id
      }]
    }
    template: {
      containers: [{
        name: 'metadata-preflight'
        image: image
        command: [
          '/app/.venv/bin/python'
          '-I'
          '-c'
          loadTextContent('../../scripts/private_runner/preflight.py')
        ]
        env: [
          {
            name: 'RUNNER_CLIENT_ID'
            value: identity.properties.clientId
          }
          {
            name: 'RUNNER_PRIVATE_IP'
            value: '10.42.2.7'
          }
        ]
        resources: {
          cpu: json('0.25')
          memory: '0.5Gi'
        }
      }]
    }
  }
  dependsOn: [
    registryPull
    metadataAssignments
  ]
}
