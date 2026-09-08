targetScope = 'resourceGroup'

@secure()
@description('Validated desired PUT snapshot; helper changes only endpoint and revision suffix. Never persist this input.')
param snapshot object

resource app 'Microsoft.App/containerApps@2025-01-01' = {
  name: 'fcag-dev-app'
  location: snapshot.location
  tags: snapshot.tags
  identity: snapshot.identity
  properties: snapshot.properties
}
