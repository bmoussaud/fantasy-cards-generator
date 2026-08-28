@description('Deployment location for the Container Apps environment.')
param location string

@description('Container Apps environment name.')
param containerAppsEnvironmentName string

@description('Log Analytics workspace customer ID for Container Apps logging.')
param logAnalyticsWorkspaceCustomerId string

@secure()
@description('Log Analytics workspace shared key for Container Apps logging.')
param logAnalyticsWorkspaceSharedKey string

@description('Delegated subnet resource ID used for workload-profile Container Apps VNet integration.')
param infrastructureSubnetId string

@description('Optional tags shared by Container Apps resources.')
param tags object = {}

resource containerAppsEnvironment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: containerAppsEnvironmentName
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalyticsWorkspaceCustomerId
        sharedKey: logAnalyticsWorkspaceSharedKey
      }
    }
    vnetConfiguration: {
      infrastructureSubnetId: infrastructureSubnetId
      internal: false
    }
    workloadProfiles: [
      {
        name: 'Consumption'
        workloadProfileType: 'Consumption'
      }
    ]
    zoneRedundant: false
  }
}

output containerAppsEnvironmentName string = containerAppsEnvironment.name
output containerAppsEnvironmentResourceId string = containerAppsEnvironment.id
output containerAppsEnvironmentDefaultDomain string = containerAppsEnvironment.properties.defaultDomain
