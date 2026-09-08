targetScope = 'resourceGroup'

@secure()
@description('Validated desired PUT snapshot; helper changes only endpoint and revision suffix. Never persist this input.')
param snapshot object

// Resource-ID list calls do not introduce a dependency on this resource's PUT.
var liveSecrets = listSecrets(resourceId('Microsoft.App/containerApps', 'fcag-dev-app'), '2025-01-01').value
var metadataSecrets = snapshot.properties.configuration.secrets
var metadataNames = map(metadataSecrets, secret => secret.name)
var liveNames = map(liveSecrets, secret => secret.name)
var allowedMetadataKeys = ['name', 'keyVaultUrl', 'identity']
var allowedLiveKeys = ['name', 'keyVaultUrl', 'identity', 'value']
var metadataInvalid = filter(metadataSecrets, secret => length(filter(items(secret), field => !contains(allowedMetadataKeys, field.key))) != 0 || empty(secret.name) || secret.name != string(secret.name) || (secret.?keyVaultUrl ?? '') != string(secret.?keyVaultUrl ?? '') || (secret.?identity ?? '') != string(secret.?identity ?? '') || (!empty(secret.?keyVaultUrl ?? '') && empty(secret.?identity ?? '')) || (empty(secret.?keyVaultUrl ?? '') && !empty(secret.?identity ?? '')))
var liveInvalid = filter(liveSecrets, secret => length(filter(items(secret), field => !contains(allowedLiveKeys, field.key))) != 0 || empty(secret.name) || secret.name != string(secret.name) || (secret.?keyVaultUrl ?? '') != string(secret.?keyVaultUrl ?? '') || (secret.?identity ?? '') != string(secret.?identity ?? '') || (empty(secret.?keyVaultUrl ?? '') && (!contains(secret, 'value') || secret.?value != string(secret.?value))))
var mismatches = filter(metadataSecrets, metadata => length(filter(liveSecrets, candidate => candidate.name == metadata.name && (candidate.?keyVaultUrl ?? '') == (metadata.?keyVaultUrl ?? '') && (candidate.?identity ?? '') == (metadata.?identity ?? ''))) != 1)
var inventoryValid = length(metadataNames) == length(union(metadataNames, metadataNames)) && length(liveNames) == length(union(liveNames, liveNames)) && length(metadataNames) == length(liveNames) && empty(metadataInvalid) && empty(liveInvalid) && empty(mismatches)

var preservedSecrets = map(metadataSecrets, metadata => !empty(metadata.?keyVaultUrl ?? '') ? metadata : union(metadata, {value: first(filter(liveSecrets, candidate => candidate.name == metadata.name)).value}))

resource app 'Microsoft.App/containerApps@2025-01-01' = {
  name: 'fcag-dev-app'
  location: snapshot.location
  tags: snapshot.tags
  identity: snapshot.identity
  // Invalid JSON is a constant, non-sensitive fail-closed expression. It is in
  // the resource input (not an output): ARM must evaluate it before any app PUT.
  properties: inventoryValid ? union(snapshot.properties, {
    configuration: union(snapshot.properties.configuration, {secrets: preservedSecrets})
  }) : json(concat('ENDPOINT_SECRET_INVENTORY_MISMATCH', take(resourceGroup().id, 0)))
}
