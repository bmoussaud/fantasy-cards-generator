@description('Deployment location for agent monitoring resources.')
param location string

@allowed([
  'dev'
  'prod'
])
@description('Environment isolated by this monitoring stack.')
param environmentName string

@description('Stable OpenTelemetry service name emitted by the hosted agent runtime.')
param agentServiceName string = 'card-orchestrator'

@description('Container App name used to filter platform logs.')
param containerAppName string

@description('Existing workspace-based Application Insights resource ID.')
param appInsightsResourceId string

@description('Existing Log Analytics workspace resource ID.')
param logAnalyticsWorkspaceResourceId string

@description('Master alert switch. Disabled by default; rules also require at least one configured receiver.')
param enableAlerts bool = false

@description('Action Group email receivers. Each item uses name, emailAddress, and useCommonAlertSchema.')
param actionGroupEmailReceivers array = []

@description('Action Group webhook receivers. Each item uses name, serviceUri, and useCommonAlertSchema. Do not embed credentials in serviceUri.')
param actionGroupWebhookReceivers array = []

@minValue(1)
@maxValue(100)
@description('Minimum invocation adverse outcomes per window to trigger the alert.')
param invocationAdverseThreshold int = 3

@minValue(1)
@maxValue(100)
@description('Minimum model provider throttles (HTTP 429) per window to trigger the alert.')
param modelThrottleThreshold int = 3

@minValue(1)
@maxValue(100)
@description('Minimum runtime exceptions per window to trigger the alert.')
param exceptionThreshold int = 5

@minValue(1)
@maxValue(100)
@description('Minimum container restart/unhealthy/probe-failure events per window to trigger the alert.')
param containerRestartThreshold int = 3

@description('Optional tags shared by agent monitoring resources.')
param tags object = {}

var resourceToken = 'fcg-agent-${environmentName}'
var hasAlertRouting = length(actionGroupEmailReceivers) > 0 || length(actionGroupWebhookReceivers) > 0
var alertsEnabled = enableAlerts && hasAlertRouting

resource actionGroup 'Microsoft.Insights/actionGroups@2023-01-01' = {
  name: take('${resourceToken}-ag', 260)
  location: 'global'
  tags: tags
  properties: {
    enabled: hasAlertRouting
    groupShortName: take('fcgagt${environmentName}', 12)
    emailReceivers: actionGroupEmailReceivers
    webhookReceivers: actionGroupWebhookReceivers
  }
}

// No Application Insights availability webtest is defined here: the hosted agent /readiness
// endpoint (port 8088) is Foundry-internal and not reachable from public test locations.
// Operational readiness is assessed via invocation outcome alerts and the runbook synthetic
// probe described in docs/agent-operational-ownership.md.

var workbookData = {
  version: 'Notebook/1.0'
  items: [
    {
      type: 1
      content: {
        json: '# card-orchestrator — ${toUpper(environmentName)} agent operations\nAgent-isolated workbook. No prompts, card text, art prompts, generated content, identity or raw client data are queried.'
      }
      name: 'overview'
    }
    {
      type: 9
      content: {
        version: 'KqlParameterItem/1.0'
        parameters: [
          {
            id: guid('workbook-agent-service', environmentName)
            version: 'KqlParameterItem/1.0'
            name: 'AgentService'
            type: 1
            isRequired: true
            value: agentServiceName
          }
          {
            id: guid('workbook-agent-version', environmentName)
            version: 'KqlParameterItem/1.0'
            name: 'AgentVersion'
            type: 1
            isRequired: true
            value: '*'
          }
          {
            id: guid('workbook-agent-session', environmentName)
            version: 'KqlParameterItem/1.0'
            name: 'SessionId'
            type: 1
            isRequired: false
            value: ''
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
        title: 'Invocation outcomes: completed, failed, timed_out, throttled'
        query: '''
AppMetrics
| where AppRoleName == '{AgentService}'
| where Name == "fcg.generation.requests"
| extend Outcome=tostring(Properties["fcg.outcome"])
| where '{AgentVersion}' == '*' or tostring(Properties["fcg.agent_version"]) == '{AgentVersion}'
| summarize Invocations=sum(Sum) by Outcome, bin(TimeGenerated, 1h)
| order by TimeGenerated asc
'''
        size: 0
        queryType: 0
        resourceType: 'microsoft.operationalinsights/workspaces'
        visualization: 'timechart'
      }
      name: 'invocation-outcomes'
    }
    {
      type: 3
      content: {
        version: 'KqlItem/1.0'
        title: 'Stage latency and failures by orchestration stage'
        query: '''
AppMetrics
| where AppRoleName == '{AgentService}'
| where Name == "fcg.generation.duration"
| extend Stage=tostring(Properties["fcg.stage"]), Outcome=tostring(Properties["fcg.outcome"])
| summarize TotalMs=sum(Sum), Count=count() by Stage, Outcome, bin(TimeGenerated, 1h)
| extend AvgMs=TotalMs / Count
| order by TimeGenerated asc
'''
        size: 0
        queryType: 0
        resourceType: 'microsoft.operationalinsights/workspaces'
        visualization: 'timechart'
      }
      name: 'stage-latency'
    }
    {
      type: 3
      content: {
        version: 'KqlItem/1.0'
        title: 'Model provider dependencies: calls, throttles (429), timeouts'
        query: '''
AppDependencies
| where AppRoleName == '{AgentService}'
| summarize Calls=count(), Failures=countif(Success == false), Throttles=countif(ResultCode == "429"), p50=percentile(DurationMs, 50), p95=percentile(DurationMs, 95) by DependencyType, bin(TimeGenerated, 1h)
| order by TimeGenerated desc
'''
        size: 0
        queryType: 0
        resourceType: 'microsoft.operationalinsights/workspaces'
      }
      name: 'model-dependencies'
    }
    {
      type: 3
      content: {
        version: 'KqlItem/1.0'
        title: 'Exceptions and bounded runtime errors'
        query: '''
union isfuzzy=true
  (AppExceptions | where AppRoleName == '{AgentService}' | project TimeGenerated, Signal="exception", Type=ExceptionType, Severity=SeverityLevel),
  (AppTraces | where AppRoleName == '{AgentService}' and SeverityLevel >= 3 | project TimeGenerated, Signal="trace", Type=tostring(Properties["fcg.error_code"]), Severity=SeverityLevel)
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
        title: 'Moderation and safety signals'
        query: '''
AppMetrics
| where AppRoleName == '{AgentService}'
| where Name == "fcg.moderation.decisions"
| extend Stage=tostring(Properties["fcg.stage"]), Reason=tostring(Properties["fcg.moderation_reason"]), Outcome=tostring(Properties["fcg.outcome"])
| summarize Decisions=sum(Sum) by Stage, Reason, Outcome, bin(TimeGenerated, 1h)
| order by TimeGenerated desc
'''
        size: 0
        queryType: 0
        resourceType: 'microsoft.operationalinsights/workspaces'
      }
      name: 'moderation'
    }
    {
      type: 3
      content: {
        version: 'KqlItem/1.0'
        title: 'ACA container restart and unhealthy events'
        query: replace('''
ContainerAppSystemLogs_CL
| where ContainerAppName_s == '__CONTAINER_APP_NAME__'
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
  ]
  isLocked: false
  fallbackResourceIds: [
    logAnalyticsWorkspaceResourceId
  ]
}

resource agentWorkbook 'Microsoft.Insights/workbooks@2022-04-01' = {
  name: guid(resourceGroup().id, environmentName, 'card-orchestrator-agent-operations')
  location: location
  kind: 'shared'
  tags: tags
  properties: {
    displayName: 'card-orchestrator ${toUpper(environmentName)} Agent Operations'
    serializedData: string(workbookData)
    version: '1.0'
    sourceId: logAnalyticsWorkspaceResourceId
    category: 'workbook'
  }
}

var invocationAdverseQuery = format('''
AppMetrics
| where AppRoleName == '{0}'
| where (Name == "fcg.generation.requests" and tostring(Properties["fcg.outcome"]) in ("failed", "timed_out", "throttled"))
    or Name == "fcg.generation.partial_results"
| summarize AdverseOutcomes=sum(Sum)
| where AdverseOutcomes >= {1}
| project Breach=1
''', agentServiceName, invocationAdverseThreshold)

var modelThrottleQuery = format('''
AppDependencies
| where AppRoleName == '{0}'
| where ResultCode == "429"
| summarize Throttles=count()
| where Throttles >= {1}
| project Breach=1
''', agentServiceName, modelThrottleThreshold)

var exceptionQuery = format('''
AppExceptions
| where AppRoleName == '{0}'
| summarize Exceptions=count()
| where Exceptions >= {1}
| project Breach=1
''', agentServiceName, exceptionThreshold)

var containerRestartQuery = format('''
ContainerAppSystemLogs_CL
| where ContainerAppName_s == '{0}'
| where Reason_s in ("Restarting", "Unhealthy", "HealthProbeFailed") or Log_s has_any ("restart", "unhealthy", "probe failed")
| summarize RestartOrUnhealthyEvents=count()
| where RestartOrUnhealthyEvents >= {1}
| project Breach=1
''', containerAppName, containerRestartThreshold)

var alertDefinitions = [
  {
    name: 'agent-invocation-adverse'
    displayName: 'Agent invocation adverse outcomes'
    description: 'card-orchestrator invocation failures, timeouts, or throttled outcomes reached the configured threshold. Check model capacity and hosted-version health.'
    severity: 1
    evaluationFrequency: 'PT5M'
    windowSize: 'PT15M'
    query: invocationAdverseQuery
  }
  {
    name: 'agent-model-throttles'
    displayName: 'Agent model provider throttling'
    description: 'Foundry model provider returned HTTP 429 for card-orchestrator requests. Verify capacity allocation and quota for the text deployment.'
    severity: 1
    evaluationFrequency: 'PT5M'
    windowSize: 'PT15M'
    query: modelThrottleQuery
  }
  {
    name: 'agent-exceptions'
    displayName: 'Agent runtime exception burst'
    description: 'card-orchestrator runtime exceptions reached the configured threshold.'
    severity: 1
    evaluationFrequency: 'PT5M'
    windowSize: 'PT15M'
    query: exceptionQuery
  }
  {
    name: 'agent-container-restarts'
    displayName: 'Agent container restart or unhealthy burst'
    description: 'ACA restart, unhealthy, or probe-failure events for the card-orchestrator container reached the configured threshold.'
    severity: 1
    evaluationFrequency: 'PT5M'
    windowSize: 'PT15M'
    query: containerRestartQuery
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
output workbookName string = agentWorkbook.name
output agentServiceName string = agentServiceName
