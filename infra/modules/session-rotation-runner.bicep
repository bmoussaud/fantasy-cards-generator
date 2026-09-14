@allowed(['dev'])
param environmentName string

@minLength(36)
@maxLength(36)
param sessionRunId string

@minLength(12)
@maxLength(12)
param sessionExpectedHash string

param existingIdentityPrincipalId string = ''

@minValue(1)
#disable-next-line BCP329 // Fail closed outside the independently reviewed dev scope.
param devScopeGuard int = resourceGroup().name == 'rg-fcag-dev' && subscription().subscriptionId == 'b8ff3e15-7e2d-4fac-a773-992fb59ccedd' ? 1 : 0

var jobName = 'fcag-${environmentName}-session-rotation'
var image = 'fcagdevqhg3qc4rlbt4gacr.azurecr.io/fantasy-cards-generator/web-nat-dev@sha256:bb7c5c4e49b9f3860d0f5aca5ccf2ff66e43921f512726551de7fc8c60ee8a11'
var acrPullRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
var sessionRoleGuid = guid(resourceGroup().id, 'private-session-rotation-v${devScopeGuard}')
var sessionRoleDefinitionId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', sessionRoleGuid)

resource environment 'Microsoft.App/managedEnvironments@2024-03-01' existing = {
  name: 'fcag-dev-cae'
}
resource app 'Microsoft.App/containerApps@2024-03-01' existing = {
  name: 'fcag-dev-app'
}
resource vault 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: 'kvfcagdevqhg3qc4rlbt4g'
}
resource sessionSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' existing = {
  parent: vault
  name: 'app-session-secret-key'
}
resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: 'fcagdevqhg3qc4rlbt4gacr'
}
resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${jobName}-id'
  location: 'eastus2'
}
resource sessionRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: sessionRoleGuid
  properties: {
    roleName: '${resourceGroup().name} temporary session rotation'
    description: 'Temporary session-only drill: get/set values and list version metadata.'
    type: 'CustomRole'
    assignableScopes: [resourceGroup().id]
    permissions: [{
      actions: []
      notActions: []
      dataActions: [
        'Microsoft.KeyVault/vaults/secrets/getSecret/action'
        'Microsoft.KeyVault/vaults/secrets/setSecret/action'
        'Microsoft.KeyVault/vaults/secrets/readMetadata/action'
      ]
      notDataActions: []
    }]
  }
}
resource sessionAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(vault.id, 'app-session-secret-key', identity.id, sessionRole.id)
  scope: sessionSecret
  properties: {
    principalId: empty(existingIdentityPrincipalId) ? identity.properties.principalId : existingIdentityPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: sessionRoleDefinitionId
  }
}
resource registryPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, identity.id, acrPullRoleId)
  scope: registry
  properties: {
    principalId: empty(existingIdentityPrincipalId) ? identity.properties.principalId : existingIdentityPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: acrPullRoleId
  }
}
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
      replicaTimeout: 4500
      registries: [{
        server: registry.properties.loginServer
        identity: identity.id
      }]
    }
    template: {
      containers: [{
        name: 'session-drill'
        image: image
        command: [
          '/app/.venv/bin/python'
          '-I'
          '-c'
          loadTextContent('../../scripts/session_rotation/harness.py')
        ]
        env: [
          { name: 'RUNNER_CLIENT_ID', value: identity.properties.clientId }
          { name: 'SESSION_RUN_ID', value: sessionRunId }
          { name: 'SESSION_EXPECTED_HASH', value: sessionExpectedHash }
          { name: 'SESSION_APP_HOST', value: app.properties.configuration.ingress.fqdn }
        ]
        resources: {
          cpu: json('0.25')
          memory: '0.5Gi'
        }
      }]
    }
  }
  dependsOn: [registryPull, sessionAssignment]
}
