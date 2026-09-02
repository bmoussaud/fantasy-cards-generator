// Cosmos DB Private Endpoint module.
//
// Deploys:
//  - privatelink.documents.azure.com private DNS zone
//  - VNet link for that zone to the provided virtual network
//  - Private endpoint targeting the Cosmos account (NoSQL group ID: Sql)
//  - Private DNS zone group wiring the endpoint to the zone
//
// GOVERNANCE CONTEXT (2026-09-02):
// Azure Policy 'CosmosDB_PublicNetwork_Modify' at Management Group
// 31b6a5c6-8762-4d6b-bf6e-f37931c67a75 keeps publicNetworkAccess: Disabled.
// Private Endpoint connections bypass publicNetworkAccess: Disabled; this is
// the durable unblocking path independent of that governance policy.
//
// This module can be deployed standalone or wired into main.bicep for azd up.

@description('Deployment location for the private endpoint and related resources.')
param location string

@description('Cosmos DB account name (used for naming derived resources).')
param cosmosAccountName string

@description('Cosmos DB account resource ID — target of the private endpoint.')
param cosmosAccountResourceId string

@description('Resource ID of the private-endpoints subnet.')
param privateEndpointSubnetResourceId string

@description('Resource ID of the virtual network to link the private DNS zone to.')
param virtualNetworkResourceId string

@description('Optional tags applied to all resources in this module.')
param tags object = {}

var cosmosSqlPrivateDnsZoneName = 'privatelink.documents.azure.com'
var privateEndpointName = take('${cosmosAccountName}-sql-pe', 80)

// Private DNS zone for Cosmos NoSQL API (group ID: Sql)
resource cosmosSqlPrivateDnsZone 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: cosmosSqlPrivateDnsZoneName
  location: 'global'
  tags: tags
}

// Link the DNS zone to the VNet so Container Apps can resolve the private FQDN
resource cosmosSqlPrivateDnsVnetLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: cosmosSqlPrivateDnsZone
  name: take('${cosmosAccountName}-sql-vnet-link', 80)
  location: 'global'
  tags: tags
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: virtualNetworkResourceId
    }
  }
}

resource cosmosPrivateEndpoint 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: privateEndpointName
  location: location
  tags: tags
  properties: {
    privateLinkServiceConnections: [
      {
        name: '${privateEndpointName}-connection'
        properties: {
          // Microsoft Learn canonical group ID for Cosmos NoSQL (SQL) API:
          // https://learn.microsoft.com/en-us/azure/cosmos-db/how-to-configure-private-endpoints
          groupIds: [
            'Sql'
          ]
          privateLinkServiceId: cosmosAccountResourceId
        }
      }
    ]
    subnet: {
      id: privateEndpointSubnetResourceId
    }
  }
}

// Zone group wires the endpoint to the DNS zone so the NIC IP is auto-registered
resource cosmosSqlPrivateDnsZoneGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = {
  parent: cosmosPrivateEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'sql'
        properties: {
          privateDnsZoneId: cosmosSqlPrivateDnsZone.id
        }
      }
    ]
  }
  dependsOn: [
    cosmosSqlPrivateDnsVnetLink
  ]
}

output cosmosPrivateEndpointResourceId string = cosmosPrivateEndpoint.id
output cosmosPrivateEndpointName string = cosmosPrivateEndpoint.name
output cosmosSqlPrivateDnsZoneName string = cosmosSqlPrivateDnsZone.name
output cosmosSqlPrivateDnsZoneResourceId string = cosmosSqlPrivateDnsZone.id
