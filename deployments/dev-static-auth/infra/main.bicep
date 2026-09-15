targetScope = 'resourceGroup'

@secure()
@description('Validated desired PUT snapshot. Never persist this input.')
param snapshot object

@secure()
param appSessionSecretKey string = ''

@secure()
param entraClientSecret string = ''

@allowed([
  'transfer'
  'deploy-image'
  'cleanup-app'
])
param phase string

@description('Exact reviewed rollback digest for deploy-image; empty otherwise.')
param rollbackImage string = ''

var liveSecrets = listSecrets(resourceId('Microsoft.App/containerApps', 'fcag-dev-app'), '2025-01-01').value
var metadataSecrets = snapshot.properties.configuration.secrets
var metadataNames = map(metadataSecrets, secret => secret.name)
var liveNames = map(liveSecrets, secret => secret.name)
var targetNames = [
  'app-session-secret-key'
  'entra-client-secret'
]
var metadataInvalid = filter(metadataSecrets, secret => length(filter(items(secret), field => !contains(['name', 'keyVaultUrl', 'identity'], field.key))) != 0 || empty(secret.name))
var liveInvalid = filter(liveSecrets, secret => length(filter(items(secret), field => !contains(['name', 'keyVaultUrl', 'identity', 'value'], field.key))) != 0 || empty(secret.name) || (empty(secret.?keyVaultUrl ?? '') && !contains(secret, 'value')))
var mismatches = filter(metadataSecrets, metadata => length(filter(liveSecrets, candidate => candidate.name == metadata.name && (candidate.?keyVaultUrl ?? '') == (metadata.?keyVaultUrl ?? '') && (candidate.?identity ?? '') == (metadata.?identity ?? ''))) != 1)
var inventoryValid = length(metadataNames) == length(union(metadataNames, metadataNames)) && length(liveNames) == length(union(liveNames, liveNames)) && length(metadataNames) == length(liveNames) && empty(metadataInvalid) && empty(liveInvalid) && empty(mismatches)
var resolvedSecrets = map(metadataSecrets, metadata => !empty(metadata.?keyVaultUrl ?? '') ? metadata : union(metadata, {
  value: first(filter(liveSecrets, candidate => candidate.name == metadata.name)).value
}))
var targetCollisionsValid = empty(filter(metadataSecrets, secret => contains(targetNames, secret.name) && !empty(secret.?keyVaultUrl ?? '')))

module app './app.bicep' = {
  name: 'dev-static-auth-app'
  params: {
    snapshot: inventoryValid && targetCollisionsValid ? union(snapshot, {
      properties: union(snapshot.properties, {
        configuration: union(snapshot.properties.configuration, {
          secrets: resolvedSecrets
        })
      })
    }) : json(concat('STATIC_AUTH_SECRET_INVENTORY_MISMATCH', take(resourceGroup().id, 0)))
    appSessionSecretKey: appSessionSecretKey
    entraClientSecret: entraClientSecret
    phase: phase
    rollbackImage: rollbackImage
  }
}
