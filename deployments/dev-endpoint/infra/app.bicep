targetScope = 'resourceGroup'

@secure()
@description('Azure-resolved desired snapshot. Never output or persist resolved values.')
param snapshot object

resource app 'Microsoft.App/containerApps@2025-01-01' = {
  name: 'fcag-dev-app'
  location: snapshot.location
  tags: snapshot.tags
  identity: snapshot.identity
  properties: snapshot.properties
}
