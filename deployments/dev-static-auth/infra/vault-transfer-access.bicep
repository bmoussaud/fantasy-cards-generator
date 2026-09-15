targetScope = 'resourceGroup'

@secure()
@description('Validated writable snapshot of the existing fixed dev vault.')
param snapshot object

@description('Temporarily permit ARM template secret resolution.')
param enableTransferAccess bool

var priorNetworkAcls = snapshot.properties.?networkAcls
var transferNetworkAcls = union(priorNetworkAcls ?? {}, {
  bypass: 'AzureServices'
  defaultAction: 'Deny'
  ipRules: priorNetworkAcls.?ipRules ?? []
  virtualNetworkRules: priorNetworkAcls.?virtualNetworkRules ?? []
})

resource vault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: 'kvfcagdevqhg3qc4rlbt4g'
  location: snapshot.location
  tags: snapshot.tags
  properties: enableTransferAccess ? union(snapshot.properties, {
    enabledForTemplateDeployment: enableTransferAccess
    networkAcls: transferNetworkAcls
  }) : snapshot.properties
}
