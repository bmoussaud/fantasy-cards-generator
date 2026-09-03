@description('Deployment location for storage resources.')
param location string

@description('Storage account name. Must be globally unique.')
param storageAccountName string

@description('Blob container used to store card assets.')
param containerName string

@description('Blob container used to store durable saved profile photos and thumbnails.')
param profilePhotosContainerName string = 'profile-photos'

@description('Managed identity principal ID for the Container App that needs Blob Storage data-plane access.')
param containerAppPrincipalId string

@description('Microsoft Entra object ID of the deployment caller that needs read-only Blob data-plane access.')
param deployerPrincipalId string

@allowed([
  'ServicePrincipal'
  'User'
])
@description('Microsoft Entra principal type of the deployment caller.')
param deployerPrincipalType string

@description('Subnet resource ID used for the Blob Storage private endpoint.')
param privateEndpointSubnetResourceId string

@description('Virtual network resource ID linked to the Blob Storage private DNS zone.')
param virtualNetworkResourceId string

@description('Optional tags shared by storage resources.')
param tags object = {}

var storageBlobDataReaderRoleDefinitionId = '2a2b9908-6ea1-4ae2-8e65-a410df84e7d1'
var storageBlobDataContributorRoleDefinitionId = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
var blobPrivateDnsZoneName = 'privatelink.blob.${environment().suffixes.storage}'
var privateEndpointName = take('${storageAccountName}-blob-pe', 80)

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageAccountName
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: {
    name: 'Standard_LRS'
  }
  properties: {
    accessTier: 'Hot'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    defaultToOAuthAuthentication: true
    minimumTlsVersion: 'TLS1_2'
    networkAcls: {
      bypass: 'AzureServices'
      defaultAction: 'Deny'
    }
    publicNetworkAccess: 'Disabled'
    sasPolicy: {
      expirationAction: 'Log'
      sasExpirationPeriod: '0.00:15:00'
    }
    supportsHttpsTrafficOnly: true
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storageAccount
  name: 'default'
}

resource cardAssetsContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: containerName
  properties: {
    publicAccess: 'None'
  }
}

resource profilePhotosContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: profilePhotosContainerName
  properties: {
    publicAccess: 'None'
  }
}

resource blobPrivateDnsZone 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: blobPrivateDnsZoneName
  location: 'global'
  tags: tags
}

resource blobPrivateDnsVnetLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: blobPrivateDnsZone
  name: take('${storageAccountName}-blob-vnet-link', 80)
  location: 'global'
  tags: tags
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: virtualNetworkResourceId
    }
  }
}

resource blobPrivateEndpoint 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: privateEndpointName
  location: location
  tags: tags
  properties: {
    privateLinkServiceConnections: [
      {
        name: '${privateEndpointName}-connection'
        properties: {
          groupIds: [
            'blob'
          ]
          privateLinkServiceId: storageAccount.id
        }
      }
    ]
    subnet: {
      id: privateEndpointSubnetResourceId
    }
  }
}

resource blobPrivateDnsZoneGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = {
  parent: blobPrivateEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'blob'
        properties: {
          privateDnsZoneId: blobPrivateDnsZone.id
        }
      }
    ]
  }
  dependsOn: [
    blobPrivateDnsVnetLink
  ]
}

resource storageBlobDataContributorRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: cardAssetsContainer
  name: guid(cardAssetsContainer.id, containerAppPrincipalId, storageBlobDataContributorRoleDefinitionId)
  properties: {
    principalId: containerAppPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageBlobDataContributorRoleDefinitionId)
  }
}

resource profilePhotosBlobDataContributorRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: profilePhotosContainer
  name: guid(profilePhotosContainer.id, containerAppPrincipalId, storageBlobDataContributorRoleDefinitionId)
  properties: {
    principalId: containerAppPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageBlobDataContributorRoleDefinitionId)
  }
}

resource deployerStorageBlobDataReaderRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storageAccount
  name: guid(storageAccount.id, deployerPrincipalId, storageBlobDataReaderRoleDefinitionId)
  properties: {
    principalId: deployerPrincipalId
    principalType: deployerPrincipalType
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageBlobDataReaderRoleDefinitionId)
  }
}

output storageAccountName string = storageAccount.name
output storageAccountResourceId string = storageAccount.id
output storageBlobEndpoint string = storageAccount.properties.primaryEndpoints.blob
output storageContainerName string = containerName
output profilePhotosContainerName string = profilePhotosContainerName
output storageBlobPrivateEndpointResourceId string = blobPrivateEndpoint.id
