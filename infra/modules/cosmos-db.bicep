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

@description('Microsoft Entra object ID of the deployment caller that needs read-only Cosmos DB data-plane access.')
param deployerPrincipalId string

@description('Static public IP address exposed by the NAT Gateway for Container Apps egress.')
param natGatewayPublicIpAddress string

@description('Optional legacy IPv4 rule to retain during cutover or rollback.')
param legacyIpRule string = ''

@description('Optional tags shared by Cosmos DB resources.')
param tags object = {}

var cosmosDataReaderRoleDefinitionId = '00000000-0000-0000-0000-000000000001'
var cosmosDataContributorRoleDefinitionId = '00000000-0000-0000-0000-000000000002'
var cosmosIpRules = empty(trim(legacyIpRule))
  ? [
      {
        ipAddressOrRange: natGatewayPublicIpAddress
      }
    ]
  : [
      {
        ipAddressOrRange: natGatewayPublicIpAddress
      }
      {
        ipAddressOrRange: trim(legacyIpRule)
      }
    ]

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
    ipRules: cosmosIpRules
    // GOVERNANCE POLICY ENFORCEMENT (2026-09-02):
    // Azure Policy 'CosmosDB_PublicNetwork_Modify' (MCAPSGovDeployPolicies, effect: modify)
    // is assigned at Management Group 31b6a5c6-8762-4d6b-bf6e-f37931c67a75 and forcibly
    // sets publicNetworkAccess: Disabled on every Cosmos account in this tenant regardless
    // of serverless/provisioned mode. Every REST PATCH to set publicNetworkAccess: Enabled
    // returns HTTP 200 / Succeeded but the value snaps back — Azure Policy re-applies Disabled
    // asynchronously. No resource locks or RG-level policy assignments are involved.
    // publicNetworkAccess: Disabled blocks ALL traffic including IP rules and VNet service
    // endpoints; the ipRules below are inert while this flag is Disabled.
    // ACTIVE UNBLOCKING PATH (deployed 2026-09-02):
    //   Private Endpoint deployed via cosmos-private-endpoint.bicep into the private-endpoints
    //   subnet (privatelink.documents.azure.com DNS zone, VNet link, zone group). PE connection
    //   status: Approved. Container App resolves *.documents.azure.com to private IPs
    //   (10.42.2.5/10.42.2.6) via the VNet-linked private DNS zone.
    //   The aca-infra subnet Microsoft.AzureCosmosDB service endpoint has been removed.
    //   ipRules are retained but inert (PNA: Disabled); can be cleaned in a future pass.
    isVirtualNetworkFilterEnabled: false
    locations: [
      {
        failoverPriority: 0
        isZoneRedundant: false
        locationName: location
      }
    ]
    networkAclBypass: 'None'
    networkAclBypassResourceIds: []
    publicNetworkAccess: 'Disabled'
    virtualNetworkRules: []
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

resource deployerCosmosDataReaderRoleAssignment 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-05-15' = {
  parent: databaseAccount
  name: guid(databaseAccount.id, deployerPrincipalId, cosmosDataReaderRoleDefinitionId)
  properties: {
    principalId: deployerPrincipalId
    roleDefinitionId: '${databaseAccount.id}/sqlRoleDefinitions/${cosmosDataReaderRoleDefinitionId}'
    scope: databaseAccount.id
  }
}

output cosmosAccountName string = databaseAccount.name
output cosmosAccountResourceId string = databaseAccount.id
output cosmosAccountEndpoint string = databaseAccount.properties.documentEndpoint
output cosmosDatabaseName string = databaseName
output cosmosContainerName string = containerName
output cosmosIpRules array = cosmosIpRules
