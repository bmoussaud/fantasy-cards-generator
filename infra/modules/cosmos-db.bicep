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

module databaseAccount 'br/public:avm/res/document-db/database-account:0.9.0' = {
  name: 'cosmos-db-account'
  params: {
    name: accountName
    location: location
    capacityMode: 'Serverless'
    defaultConsistencyLevel: 'Session'
    disableKeyBasedMetadataWriteAccess: true
    disableLocalAuthentication: true
    enableAutomaticFailover: false
    enableMultipleWriteLocations: false
    networkRestrictions: {
      publicNetworkAccess: 'Enabled'
    }
    sqlDatabases: [
      {
        name: databaseName
        containers: [
          {
            kind: 'Hash'
            name: containerName
            paths: [
              '/userId'
            ]
          }
        ]
      }
    ]
    sqlRoleAssignments: [
      {
        principalId: containerAppPrincipalId
        roleDefinitionId: '00000000-0000-0000-0000-000000000002'
        scope: '/dbs/${databaseName}/colls/${containerName}'
      }
    ]
    zoneRedundant: false
    tags: tags
  }
}

output cosmosAccountName string = databaseAccount.outputs.name
output cosmosAccountResourceId string = databaseAccount.outputs.resourceId
output cosmosAccountEndpoint string = databaseAccount.outputs.endpoint
output cosmosDatabaseName string = databaseName
output cosmosContainerName string = containerName
