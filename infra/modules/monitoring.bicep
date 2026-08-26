@description('Deployment location for monitoring resources.')
param location string

@description('Log Analytics workspace name.')
param logAnalyticsWorkspaceName string

@description('Application Insights resource name.')
param appInsightsName string

@description('Optional tags shared by monitoring resources.')
param tags object = {}

module logAnalyticsWorkspace 'br/public:avm/res/operational-insights/workspace:0.15.0' = {
  name: 'log-analytics-workspace'
  params: {
    name: logAnalyticsWorkspaceName
    location: location
    dataRetention: 30
    tags: tags
  }
}

module applicationInsights 'br/public:avm/res/insights/component:0.7.1' = {
  name: 'application-insights'
  params: {
    name: appInsightsName
    location: location
    workspaceResourceId: logAnalyticsWorkspace.outputs.resourceId
    applicationType: 'web'
    tags: tags
  }
}

output appInsightsConnectionString string = applicationInsights.outputs.connectionString
output appInsightsName string = applicationInsights.outputs.name
output logAnalyticsWorkspaceName string = logAnalyticsWorkspace.outputs.name
output logAnalyticsWorkspaceResourceId string = logAnalyticsWorkspace.outputs.resourceId
