@description('Deployment location for network resources.')
param location string

@description('Virtual Network name.')
param virtualNetworkName string

@description('Address prefix for the Virtual Network.')
param virtualNetworkAddressPrefix string = '10.42.0.0/16'

@description('Dedicated delegated subnet name for the workload-profile Container Apps environment.')
param containerAppsSubnetName string

@description('Address prefix for the Container Apps infrastructure subnet.')
param containerAppsSubnetAddressPrefix string = '10.42.0.0/23'

@description('Dedicated subnet name for private endpoints consumed by Container Apps.')
param privateEndpointSubnetName string = 'private-endpoints'

@description('Address prefix for the private endpoint subnet.')
param privateEndpointSubnetAddressPrefix string = '10.42.2.0/24'

@description('NAT Gateway name.')
param natGatewayName string

@description('Static public IP resource name used by the NAT Gateway.')
param natGatewayPublicIpName string

@description('Optional tags shared by network resources.')
param tags object = {}

resource natGatewayPublicIp 'Microsoft.Network/publicIPAddresses@2024-05-01' = {
  name: natGatewayPublicIpName
  location: location
  sku: {
    name: 'Standard'
  }
  tags: tags
  properties: {
    publicIPAllocationMethod: 'Static'
    publicIPAddressVersion: 'IPv4'
  }
}

resource natGateway 'Microsoft.Network/natGateways@2024-05-01' = {
  name: natGatewayName
  location: location
  sku: {
    name: 'Standard'
  }
  tags: tags
  properties: {
    idleTimeoutInMinutes: 4
    publicIpAddresses: [
      {
        id: natGatewayPublicIp.id
      }
    ]
  }
}

resource virtualNetwork 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: virtualNetworkName
  location: location
  tags: tags
  properties: {
    addressSpace: {
      addressPrefixes: [
        virtualNetworkAddressPrefix
      ]
    }
    subnets: [
      {
        name: containerAppsSubnetName
        properties: {
          addressPrefix: containerAppsSubnetAddressPrefix
          delegations: [
            {
              name: 'container-apps-environment'
              properties: {
                serviceName: 'Microsoft.App/environments'
              }
            }
          ]
          natGateway: {
            id: natGateway.id
          }
        }
      }
      {
        name: privateEndpointSubnetName
        properties: {
          addressPrefix: privateEndpointSubnetAddressPrefix
          privateEndpointNetworkPolicies: 'Disabled'
        }
      }
    ]
  }
}

resource containerAppsSubnet 'Microsoft.Network/virtualNetworks/subnets@2024-05-01' existing = {
  parent: virtualNetwork
  name: containerAppsSubnetName
}

resource privateEndpointSubnet 'Microsoft.Network/virtualNetworks/subnets@2024-05-01' existing = {
  parent: virtualNetwork
  name: privateEndpointSubnetName
}

output virtualNetworkName string = virtualNetwork.name
output virtualNetworkResourceId string = virtualNetwork.id
output containerAppsSubnetName string = containerAppsSubnet.name
output containerAppsSubnetResourceId string = containerAppsSubnet.id
output privateEndpointSubnetName string = privateEndpointSubnet.name
output privateEndpointSubnetResourceId string = privateEndpointSubnet.id
output natGatewayName string = natGateway.name
output natGatewayResourceId string = natGateway.id
output natGatewayPublicIpName string = natGatewayPublicIp.name
output natGatewayPublicIpResourceId string = natGatewayPublicIp.id
output natGatewayPublicIpAddress string = natGatewayPublicIp.properties.ipAddress
