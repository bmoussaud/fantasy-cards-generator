// Runtime Key Vault access requires a private endpoint while public access is disabled.
// Keep this module independently deployable without changing the vault or its secrets.

@description('Deployment location for the private endpoint and related resources.')
param location string

@description('Key Vault name (used to derive resource names).')
param keyVaultName string

@description('Key Vault resource ID targeted by the private endpoint.')
param keyVaultResourceId string

@description('Resource ID of the private-endpoints subnet.')
param privateEndpointSubnetResourceId string

@description('Resource ID of the virtual network to link the private DNS zone to.')
param virtualNetworkResourceId string

@description('Optional tags applied to all resources in this module.')
param tags object = {}

var kvPrivateDnsZoneName = 'privatelink.vaultcore.azure.net'
var privateEndpointName = take('${keyVaultName}-vault-pe', 64)

module kvPrivateDnsZone 'br/public:avm/res/network/private-dns-zone:0.8.1' = {
  name: '${keyVaultName}-vault-dns'
  params: {
    name: kvPrivateDnsZoneName
    tags: tags
    enableTelemetry: false
    virtualNetworkLinks: [
      {
        name: take('${keyVaultName}-vault-vnet-link', 80)
        virtualNetworkResourceId: virtualNetworkResourceId
        registrationEnabled: false
      }
    ]
  }
}

module kvPrivateEndpoint 'br/public:avm/res/network/private-endpoint:0.12.1' = {
  name: privateEndpointName
  params: {
    name: privateEndpointName
    location: location
    tags: tags
    subnetResourceId: privateEndpointSubnetResourceId
    enableTelemetry: false
    privateLinkServiceConnections: [
      {
        name: '${privateEndpointName}-connection'
        properties: {
          groupIds: [
            'vault'
          ]
          privateLinkServiceId: keyVaultResourceId
        }
      }
    ]
    privateDnsZoneGroup: {
      name: 'default'
      privateDnsZoneGroupConfigs: [
        {
          name: 'vault'
          privateDnsZoneResourceId: kvPrivateDnsZone.outputs.resourceId
        }
      ]
    }
  }
}

output kvPrivateEndpointName string = kvPrivateEndpoint.outputs.name
output kvPrivateDnsZoneName string = kvPrivateDnsZone.outputs.name
output kvPrivateDnsZoneResourceId string = kvPrivateDnsZone.outputs.resourceId
