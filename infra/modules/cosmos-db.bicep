@description('Deployment location for Cosmos DB resources.')
param location string

@description('Cosmos DB account name. Must be globally unique.')
param accountName string

@description('Cosmos DB SQL database name.')
param databaseName string

@description('Cosmos DB SQL container name.')
param containerName string

@description('Managed identity principal ID for the Container App that needs Cosmos DB data-plane access.')
param containerAppPrincipalId string

@description('Optional tags shared by Cosmos DB resources.')
param tags object = {}

var cosmosDataContributorRoleDefinitionId = '00000000-0000-0000-0000-000000000002'

resource databaseAccount 'Microsoft.DocumentDB/databaseAccounts@2024-05-15' = {
  name: accountName
  location: location
  tags: tags
  kind: 'GlobalDocumentDB'
  properties: {
    capabilities: [
      {
        name: 'EnableServerless'
      }
    ]
    consistencyPolicy: {
      defaultConsistencyLevel: 'Session'
    }
    databaseAccountOfferType: 'Standard'
    disableKeyBasedMetadataWriteAccess: true
    disableLocalAuth: true
    enableAutomaticFailover: false
    enableMultipleWriteLocations: false
    locations: [
      {
        failoverPriority: 0
        isZoneRedundant: false
        locationName: location
      }
    ]
    publicNetworkAccess: 'Enabled'
  }
}

resource sqlDatabase 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2024-05-15' = {
  parent: databaseAccount
  name: databaseName
  properties: {
    resource: {
      id: databaseName
    }
  }
}

resource sqlContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-05-15' = {
  parent: sqlDatabase
  name: containerName
  properties: {
    resource: {
      defaultTtl: -1
      id: containerName
      partitionKey: {
        kind: 'Hash'
        paths: [
          '/userId'
        ]
      }
    }
  }
}

resource sqlRoleAssignment 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-05-15' = {
  parent: databaseAccount
  name: guid(databaseAccount.id, containerAppPrincipalId, databaseName, containerName, cosmosDataContributorRoleDefinitionId)
  properties: {
    principalId: containerAppPrincipalId
    roleDefinitionId: '${databaseAccount.id}/sqlRoleDefinitions/${cosmosDataContributorRoleDefinitionId}'
    scope: '${databaseAccount.id}/dbs/${databaseName}/colls/${containerName}'
  }
  dependsOn: [
    sqlContainer
  ]
}

output cosmosAccountName string = databaseAccount.name
output cosmosAccountResourceId string = databaseAccount.id
output cosmosAccountEndpoint string = databaseAccount.properties.documentEndpoint
output cosmosDatabaseName string = databaseName
output cosmosContainerName string = containerName
