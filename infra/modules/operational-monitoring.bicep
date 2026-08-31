@description('Deployment location for operational monitoring resources.')
param location string

@allowed([
  'dev'
  'prod'
])
@description('Environment isolated by this monitoring stack.')
param environmentName string

@description('Stable OpenTelemetry service name.')
param serviceName string

@description('Container App name used to filter platform logs.')
param containerAppName string

@description('Public Container App URL without a trailing slash.')
param containerAppUrl string

@description('Existing workspace-based Application Insights resource ID.')
param appInsightsResourceId string

@description('Existing Log Analytics workspace resource ID.')
param logAnalyticsWorkspaceResourceId string

@description('Configured workspace ingestion cap in GB/day.')
param dailyQuotaGb string = '0.25'

@minValue(1)
@maxValue(100)
@description('Ingestion percentage that triggers the cap-risk alert.')
param ingestionWarningPercent int = 80

@description('Master alert switch. Disabled by default; rules also require at least one configured receiver.')
param enableAlerts bool = false

@description('Action Group email receivers. Each item uses name, emailAddress, and useCommonAlertSchema.')
param actionGroupEmailReceivers array = []

@description('Action Group webhook receivers. Each item uses name, serviceUri, and useCommonAlertSchema. Do not embed credentials in serviceUri.')
param actionGroupWebhookReceivers array = []

@description('Azure availability-test location IDs.')
param availabilityTestLocations array = [
  'emea-nl-ams-azr'
  'us-va-ash-azr'
]

@minValue(1)
param availabilityFailureThreshold int = 2

@minValue(1)
@maxValue(100)
param requestFailurePercentThreshold int = 5

@minValue(1)
param requestTrafficFloor int = 5

@minValue(1)
param requestP95LatencyMsThreshold int = 10000

@minValue(1)
param dependencyFailureThreshold int = 5

@minValue(1)
param exceptionThreshold int = 5

@minValue(1)
param generationAdverseOutcomeThreshold int = 3

@minValue(1)
param containerRestartThreshold int = 3

@description('Optional tags shared by operational monitoring resources.')
param tags object = {}

var resourceToken = 'fcg-${environmentName}'
var availabilityTestName = take('${resourceToken}-healthz', 64)
var hasAlertRouting = length(actionGroupEmailReceivers) > 0 || length(actionGroupWebhookReceivers) > 0
var alertsEnabled = enableAlerts && hasAlertRouting
var ingestionWarningGb = json(dailyQuotaGb) * ingestionWarningPercent / json('100.0')
var appInsightsHiddenLinkTag = 'hidden-link:${appInsightsResourceId}'

resource actionGroup 'Microsoft.Insights/actionGroups@2023-01-01' = {
  name: take('${resourceToken}-operations-ag', 260)
  location: 'global'
  tags: tags
  properties: {
    enabled: hasAlertRouting
    groupShortName: take('fcg${environmentName}', 12)
    emailReceivers: actionGroupEmailReceivers
    webhookReceivers: actionGroupWebhookReceivers
  }
}

resource availabilityTest 'Microsoft.Insights/webtests@2022-06-15' = {
  name: availabilityTestName
  location: location
  kind: 'standard'
  tags: union(tags, {
    '${appInsightsHiddenLinkTag}': 'Resource'
  })
  properties: {
    SyntheticMonitorId: availabilityTestName
    Name: availabilityTestName
    Description: 'Environment-isolated /healthz availability check for ${serviceName}.'
    Enabled: true
    Frequency: 300
    Timeout: 30
    Kind: 'standard'
    RetryEnabled: true
    Locations: [
      for testLocation in availabilityTestLocations: {
        Id: testLocation
      }
    ]
    Request: {
      RequestUrl: '${containerAppUrl}/healthz'
      HttpVerb: 'GET'
      ParseDependentRequests: false
      FollowRedirects: false
    }
    ValidationRules: {
      ExpectedHttpStatusCode: 200
      IgnoreHttpStatusCode: false
      SSLCheck: true
      SSLCertRemainingLifetimeCheck: 30
    }
  }
}

var workbookData = {
  version: 'Notebook/1.0'
  items: [
    {
      type: 1
      content: {
        json: '# Fantasy Cards Generator — ${toUpper(environmentName)} operations\nEnvironment-isolated workbook. No prompts, generated content, identity, or raw client network data are queried.'
      }
      name: 'overview'
    }
    {
      type: 9
      content: {
        version: 'KqlParameterItem/1.0'
        parameters: [
          {
            id: guid('workbook-service', environmentName)
            version: 'KqlParameterItem/1.0'
            name: 'Service'
            type: 1
            isRequired: true
            value: serviceName
          }
          {
            id: guid('workbook-revision', environmentName)
            version: 'KqlParameterItem/1.0'
            name: 'Revision'
            type: 1
            isRequired: true
            value: '*'
          }
        ]
        style: 'pills'
        queryType: 0
        resourceType: 'microsoft.operationalinsights/workspaces'
      }
      name: 'filters'
    }
    {
      type: 3
      content: {
        version: 'KqlItem/1.0'
        title: 'Requests: volume, failures, and latency percentiles'
        query: '''
AppRequests
| where AppRoleName == '{Service}'
| summarize Requests=count(), Failures=countif(Success == false), p50=percentile(DurationMs, 50), p95=percentile(DurationMs, 95), p99=percentile(DurationMs, 99) by bin(TimeGenerated, 1h)
| order by TimeGenerated asc
'''
        size: 0
        queryType: 0
        resourceType: 'microsoft.operationalinsights/workspaces'
        visualization: 'timechart'
      }
      name: 'requests'
    }
    {
      type: 3
      content: {
        version: 'KqlItem/1.0'
        title: 'Dependencies: success and latency'
        query: '''
AppDependencies
| where AppRoleName == '{Service}'
| summarize Calls=count(), Failures=countif(Success == false), Throttles=countif(ResultCode == "429"), p50=percentile(DurationMs, 50), p95=percentile(DurationMs, 95), p99=percentile(DurationMs, 99) by DependencyType, bin(TimeGenerated, 1h)
| order by TimeGenerated desc
'''
        size: 0
        queryType: 0
        resourceType: 'microsoft.operationalinsights/workspaces'
      }
      name: 'dependencies'
    }
    {
      type: 3
      content: {
        version: 'KqlItem/1.0'
        title: 'Exceptions and bounded application errors'
        query: '''
union isfuzzy=true
  (AppExceptions | where AppRoleName == '{Service}' | project TimeGenerated, Signal="exception", Type=ExceptionType, Severity=SeverityLevel),
  (AppTraces | where AppRoleName == '{Service}' and SeverityLevel >= 3 | project TimeGenerated, Signal="trace", Type=tostring(Properties["fcg.error_code"]), Severity=SeverityLevel)
| order by TimeGenerated desc
| take 100
'''
        size: 0
        queryType: 0
        resourceType: 'microsoft.operationalinsights/workspaces'
      }
      name: 'errors'
    }
    {
      type: 3
      content: {
        version: 'KqlItem/1.0'
        title: 'Generation, moderation, retries, persistence, and token aggregates'
        query: '''
AppMetrics
| where AppRoleName == '{Service}'
| where Name startswith "fcg."
| extend Dimension = case(
    Name in ("fcg.generation.requests", "fcg.generation.duration", "fcg.artwork.retries"), strcat("outcome=", tostring(Properties["fcg.outcome"])),
    Name == "fcg.generation.partial_results", strcat("partial=", tostring(Properties["fcg.partial_reason"])),
    Name in ("fcg.dependency.attempts", "fcg.dependency.duration"), strcat("dependency=", tostring(Properties["fcg.dependency"]), ";attempt=", tostring(Properties["fcg.attempt"]), ";outcome=", tostring(Properties["fcg.outcome"])),
    Name in ("fcg.dependency.throttles", "fcg.dependency.timeouts"), strcat("dependency=", tostring(Properties["fcg.dependency"])),
    Name == "fcg.moderation.decisions", strcat("stage=", tostring(Properties["fcg.stage"]), ";moderation=", tostring(Properties["fcg.moderation_reason"]), ";outcome=", tostring(Properties["fcg.outcome"])),
    Name == "fcg.persistence.operations", strcat("store=", tostring(Properties["fcg.store"]), ";operation=", tostring(Properties["fcg.persistence_operation"]), ";outcome=", tostring(Properties["fcg.outcome"])),
    Name == "fcg.ai.tokens", strcat("operation=", tostring(Properties["fcg.operation"]), ";token_type=", tostring(Properties["fcg.token_type"])),
    "aggregate")
| summarize Value=sum(Sum) by Name, Dimension, bin(TimeGenerated, 1h)
| order by TimeGenerated desc
'''
        size: 0
        queryType: 0
        resourceType: 'microsoft.operationalinsights/workspaces'
        visualization: 'timechart'
      }
      name: 'generation'
    }
    {
      type: 3
      content: {
        version: 'KqlItem/1.0'
        title: 'ACA revision, restart, and platform errors'
        query: replace('''
ContainerAppSystemLogs_CL
| where ContainerAppName_s == '__CONTAINER_APP_NAME__'
| where '{Revision}' == '*' or RevisionName_s == '{Revision}'
| where Reason_s in ("Restarting", "Unhealthy", "HealthProbeFailed") or Log_s has_any ("restart", "unhealthy", "probe failed")
| project TimeGenerated, RevisionName_s, Reason_s, Log_s
| order by TimeGenerated desc
| take 100
''', '__CONTAINER_APP_NAME__', containerAppName)
        size: 0
        queryType: 0
        resourceType: 'microsoft.operationalinsights/workspaces'
      }
      name: 'aca-health'
    }
    {
      type: 3
      content: {
        version: 'KqlItem/1.0'
        title: 'Billable ingestion and daily-cap utilization'
        query: replace(replace('''
Usage
| where IsBillable == true
| summarize BillableGB=sum(Quantity) / 1000.0 by bin(TimeGenerated, 1d)
| extend DailyCapGB=__DAILY_QUOTA_GB__, CapUtilizationPercent=100.0 * BillableGB / __DAILY_QUOTA_GB__
| order by TimeGenerated desc
''', '__DAILY_QUOTA_GB__', string(dailyQuotaGb)), '__DAILY_QUOTA_GB__', string(dailyQuotaGb))
        size: 0
        queryType: 0
        resourceType: 'microsoft.operationalinsights/workspaces'
      }
      name: 'ingestion'
    }
  ]
  isLocked: false
  fallbackResourceIds: [
    logAnalyticsWorkspaceResourceId
  ]
}

resource operationsWorkbook 'Microsoft.Insights/workbooks@2022-04-01' = {
  name: guid(resourceGroup().id, environmentName, 'fantasy-cards-operations')
  location: location
  kind: 'shared'
  tags: tags
  properties: {
    displayName: 'Fantasy Cards ${toUpper(environmentName)} Operations'
    serializedData: string(workbookData)
    version: '1.0'
    sourceId: logAnalyticsWorkspaceResourceId
    category: 'workbook'
  }
}

var availabilityQuery = format('''
AppAvailabilityResults
| where Name == '{0}' and Success == false
| summarize Failures=count()
| where Failures >= {1}
| project Breach=1
''', availabilityTestName, availabilityFailureThreshold)

var requestFailureQuery = format('''
AppRequests
| where AppRoleName == '{0}'
| summarize Total=count(), Failed=countif(Success == false or ResultCode startswith "5")
| where Total >= {1} and (100.0 * Failed / Total) >= {2}
| project Breach=1
''', serviceName, requestTrafficFloor, requestFailurePercentThreshold)

var requestLatencyQuery = format('''
AppRequests
| where AppRoleName == '{0}'
| summarize Total=count(), P95=percentile(DurationMs, 95)
| where Total >= {1} and P95 >= {2}
| project Breach=1
''', serviceName, requestTrafficFloor, requestP95LatencyMsThreshold)

var dependencyFailureQuery = format('''
AppDependencies
| where AppRoleName == '{0}'
| where Success == false or ResultCode in ("408", "429", "504")
| summarize Failures=count()
| where Failures >= {1}
| project Breach=1
''', serviceName, dependencyFailureThreshold)

var exceptionQuery = format('''
AppExceptions
| where AppRoleName == '{0}'
| summarize Exceptions=count()
| where Exceptions >= {1}
| project Breach=1
''', serviceName, exceptionThreshold)

var generationAdverseQuery = format('''
AppMetrics
| where AppRoleName == '{0}'
| where (Name == "fcg.generation.requests" and tostring(Properties["fcg.outcome"]) in ("failed", "timed_out", "throttled"))
    or Name == "fcg.generation.partial_results"
    or (Name == "fcg.persistence.operations" and tostring(Properties["fcg.outcome"]) != "completed")
| summarize AdverseOutcomes=sum(Sum)
| where AdverseOutcomes >= {1}
| project Breach=1
''', serviceName, generationAdverseOutcomeThreshold)

var containerRestartQuery = format('''
ContainerAppSystemLogs_CL
| where ContainerAppName_s == '{0}'
| where Reason_s in ("Restarting", "Unhealthy", "HealthProbeFailed") or Log_s has_any ("restart", "unhealthy", "probe failed")
| summarize RestartOrUnhealthyEvents=count()
| where RestartOrUnhealthyEvents >= {1}
| project Breach=1
''', containerAppName, containerRestartThreshold)

var ingestionCapQuery = format('''
Usage
| where IsBillable == true
| where TimeGenerated >= ago(24h)
| summarize BillableGB=sum(Quantity) / 1000.0
| where BillableGB >= {0}
| project Breach=1
''', ingestionWarningGb)

var alertDefinitions = [
  {
    name: 'availability'
    displayName: 'Health endpoint unavailable'
    description: '/healthz availability failures reached the configured threshold.'
    severity: 1
    evaluationFrequency: 'PT5M'
    windowSize: 'PT15M'
    query: availabilityQuery
  }
  {
    name: 'request-failures'
    displayName: 'Elevated request failure ratio'
    description: 'Request failures exceeded the configured ratio and traffic floor.'
    severity: 1
    evaluationFrequency: 'PT5M'
    windowSize: 'PT15M'
    query: requestFailureQuery
  }
  {
    name: 'request-latency'
    displayName: 'Sustained request p95 latency'
    description: 'Request p95 latency exceeded the configured threshold and traffic floor.'
    severity: 2
    evaluationFrequency: 'PT5M'
    windowSize: 'PT15M'
    query: requestLatencyQuery
  }
  {
    name: 'dependency-failures'
    displayName: 'Dependency failure burst'
    description: 'Dependency failures, throttles, or timeouts reached the configured threshold.'
    severity: 1
    evaluationFrequency: 'PT5M'
    windowSize: 'PT15M'
    query: dependencyFailureQuery
  }
  {
    name: 'exceptions'
    displayName: 'Exception burst'
    description: 'Application exceptions reached the configured threshold.'
    severity: 1
    evaluationFrequency: 'PT5M'
    windowSize: 'PT15M'
    query: exceptionQuery
  }
  {
    name: 'generation-adverse'
    displayName: 'Generation adverse outcomes'
    description: 'Generation failures, partial results, or persistence failures reached the configured threshold.'
    severity: 1
    evaluationFrequency: 'PT5M'
    windowSize: 'PT15M'
    query: generationAdverseQuery
  }
  {
    name: 'container-restarts'
    displayName: 'Container restart or unhealthy burst'
    description: 'ACA restart, unhealthy, or probe-failure events reached the configured threshold.'
    severity: 1
    evaluationFrequency: 'PT5M'
    windowSize: 'PT15M'
    query: containerRestartQuery
  }
  {
    name: 'ingestion-cap'
    displayName: 'Telemetry ingestion approaching cap'
    description: 'Billable ingestion over 24 hours reached the configured percentage of the daily workspace cap.'
    severity: 2
    evaluationFrequency: 'PT1H'
    windowSize: 'P1D'
    query: ingestionCapQuery
  }
]

resource alertRules 'Microsoft.Insights/scheduledQueryRules@2023-12-01' = [
  for alertDefinition in alertDefinitions: {
    name: take('${resourceToken}-${alertDefinition.name}', 260)
    location: location
    tags: tags
    properties: {
      displayName: '${toUpper(environmentName)} ${alertDefinition.displayName}'
      description: alertDefinition.description
      severity: alertDefinition.severity
      enabled: alertsEnabled
      evaluationFrequency: alertDefinition.evaluationFrequency
      windowSize: alertDefinition.windowSize
      scopes: [
        logAnalyticsWorkspaceResourceId
      ]
      targetResourceTypes: [
        'Microsoft.OperationalInsights/workspaces'
      ]
      criteria: {
        allOf: [
          {
            query: alertDefinition.query
            timeAggregation: 'Count'
            operator: 'GreaterThan'
            threshold: 0
            failingPeriods: {
              numberOfEvaluationPeriods: 1
              minFailingPeriodsToAlert: 1
            }
          }
        ]
      }
      autoMitigate: true
      skipQueryValidation: true
      actions: {
        actionGroups: [
          actionGroup.id
        ]
      }
    }
  }
]

output actionGroupName string = actionGroup.name
output alertsEnabled bool = alertsEnabled
output availabilityTestName string = availabilityTest.name
output workbookName string = operationsWorkbook.name
