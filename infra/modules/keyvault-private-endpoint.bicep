// Key Vault Private Endpoint module.
//
// Deploys:
//  - privatelink.vaultcore.azure.net private DNS zone (native)
//  - VNet link for that zone to the provided virtual network (native)
//  - Private endpoint targeting the Key Vault (AVM avm/res/network/private-endpoint:0.12.1)
//    which also provisions the private DNS zone group
//
// WHY AVM for the private endpoint:
//   az bicep build confirms br/public:avm/res/network/private-endpoint:0.12.1 resolves
//   cleanly in this environment. AVM is preferred per team policy. The PE module handles
//   both the endpoint resource and the DNS zone group wiring. Telemetry is disabled to
//   keep the deployment surface minimal. The DNS zone and VNet link use native resources
//   (consistent with the existing cosmos-private-endpoint.bicep pattern) because AVM does
//   not own those sibling resource types.
//
// WHY private endpoint (not networkAcls / VNet service endpoint):
//   The Key Vault is deployed with publicNetworkAccess: 'Disabled' and no networkAcls.
//   Requests from the ACA container egress via NAT Gateway (public IP) are rejected with
//   HTTP 403 by the Key Vault network firewall. A private endpoint in the private-endpoints
//   subnet lets the ACA container resolve the vault FQDN to a private IP within the VNet,
//   bypassing the public network firewall entirely. This is the same pattern used for
//   Cosmos DB and Blob Storage.
//
// RBAC NOTE (correcting prior misconception in issue #103 / PR #107):
//   Key Vault Secrets User (role ID 4633458b-17de-408a-b874-0445c86b69e6) grants:
//     - Microsoft.KeyVault/vaults/secrets/getSecret/action   (read current value)
//     - Microsoft.KeyVault/vaults/secrets/readMetadata/action (list versions, read props)
//   readMetadata covers secret version enumeration. The original issue #103 suspected a
//   missing list-versions permission; this was not the cause. The startup failure was
//   purely a network connectivity gap (no private endpoint → HTTP 403 from vault firewall).
//   No additional RBAC grant is needed; Key Vault Secrets User is sufficient.

@description('Deployment location for the private endpoint and related resources.')
param location string

@description('Key Vault name (used to derive resource names).')
param keyVaultName string

@description('Key Vault resource ID — target of the private endpoint.')
param keyVaultResourceId string

@description('Resource ID of the private-endpoints subnet.')
param privateEndpointSubnetResourceId string

@description('Resource ID of the virtual network to link the private DNS zone to.')
param virtualNetworkResourceId string

@description('Optional tags applied to all resources in this module.')
param tags object = {}

var kvPrivateDnsZoneName = 'privatelink.vaultcore.azure.net'
var privateEndpointName = take('${keyVaultName}-vault-pe', 64)

// Private DNS zone for Key Vault data plane
resource kvPrivateDnsZone 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: kvPrivateDnsZoneName
  location: 'global'
  tags: tags
}

// Link the DNS zone to the VNet so Container Apps can resolve the private FQDN
resource kvPrivateDnsVnetLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: kvPrivateDnsZone
  name: take('${keyVaultName}-vault-vnet-link', 80)
  location: 'global'
  tags: tags
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: virtualNetworkResourceId
    }
  }
}

// Private endpoint (AVM module: handles PE resource + DNS zone group).
// enableTelemetry: false — no anonymous usage reporting from this deployment.
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
          // Microsoft Learn canonical group ID for Key Vault data plane:
          // https://learn.microsoft.com/en-us/azure/key-vault/general/private-link-service
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
          privateDnsZoneResourceId: kvPrivateDnsZone.id
        }
      ]
    }
  }
  dependsOn: [
    kvPrivateDnsVnetLink
  ]
}

output kvPrivateEndpointName string = kvPrivateEndpoint.outputs.name
output kvPrivateDnsZoneName string = kvPrivateDnsZone.name
output kvPrivateDnsZoneResourceId string = kvPrivateDnsZone.id
