@description('Deployment location for storage resources.')
param location string

@description('Storage account name. Must be globally unique.')
param storageAccountName string

@description('Blob container used to store card assets.')
param containerName string

@description('Managed identity principal ID for the Container App that needs Blob Storage data-plane access.')
param containerAppPrincipalId string

@description('Optional tags shared by storage resources.')
param tags object = {}

module storageAccount 'br/public:avm/res/storage/storage-account:0.9.1' = {
  name: 'storage-account'
  params: {
    name: storageAccountName
    location: location
    accessTier: 'Hot'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    blobServices: {
      containers: [
        {
          name: containerName
          publicAccess: 'None'
        }
      ]
    }
    defaultToOAuthAuthentication: true
    kind: 'StorageV2'
    publicNetworkAccess: 'Enabled'
    roleAssignments: [
      {
        principalId: containerAppPrincipalId
        principalType: 'ServicePrincipal'
        roleDefinitionIdOrName: 'Storage Blob Data Contributor'
      }
    ]
    sasExpirationPeriod: '0.00:15:00'
    skuName: 'Standard_LRS'
    tags: tags
  }
}

output storageAccountName string = storageAccount.outputs.name
output storageAccountResourceId string = storageAccount.outputs.resourceId
output storageBlobEndpoint string = storageAccount.outputs.primaryBlobEndpoint
output storageContainerName string = containerName
