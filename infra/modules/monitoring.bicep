@description('Deployment location for monitoring resources.')
param location string

@description('Log Analytics workspace name.')
param logAnalyticsWorkspaceName string

@description('Application Insights resource name.')
param appInsightsName string

@minValue(30)
@maxValue(730)
@description('Log Analytics retention in days.')
param retentionInDays int = 30

@description('Log Analytics daily ingestion cap in GB, represented as a decimal string for ARM/Bicep compatibility.')
param dailyQuotaGb string = '0.25'

@description('Optional tags shared by monitoring resources.')
param tags object = {}

resource logAnalyticsWorkspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: logAnalyticsWorkspaceName
  location: location
  tags: tags
  properties: {
    retentionInDays: retentionInDays
    sku: {
      name: 'PerGB2018'
    }
    workspaceCapping: {
      dailyQuotaGb: json(dailyQuotaGb)
    }
  }
}

resource applicationInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: appInsightsName
  location: location
  kind: 'web'
  tags: tags
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalyticsWorkspace.id
    DisableIpMasking: false
    IngestionMode: 'LogAnalytics'
  }
}

output appInsightsConnectionString string = applicationInsights.properties.ConnectionString
output appInsightsName string = applicationInsights.name
output appInsightsResourceId string = applicationInsights.id
output dailyQuotaGb string = dailyQuotaGb
output logAnalyticsWorkspaceCustomerId string = logAnalyticsWorkspace.properties.customerId
output logAnalyticsWorkspaceName string = logAnalyticsWorkspace.name
output logAnalyticsWorkspaceResourceId string = logAnalyticsWorkspace.id
@secure()
output logAnalyticsWorkspaceSharedKey string = logAnalyticsWorkspace.listKeys().primarySharedKey
