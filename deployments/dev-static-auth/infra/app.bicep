targetScope = 'resourceGroup'

@secure()
@description('Azure-resolved full writable snapshot with existing native secret values.')
param snapshot object

@secure()
param appSessionSecretKey string = ''

@secure()
param entraClientSecret string = ''

@allowed([
  'transfer'
  'deploy-image'
  'cleanup-app'
])
param phase string

param rollbackImage string = ''

var rotationOnlyEnvironmentNames = [
  'SECRET_PROVIDER_BACKEND'
  'SECRET_PROVIDER_CACHE_TTL_SECONDS'
  'SECRET_PROVIDER_REQUEST_TIMEOUT_SECONDS'
  'SECRET_PROVIDER_MAX_RETRIES'
  'SECRET_PROVIDER_RETRY_BACKOFF_SECONDS'
  'SECRET_PROVIDER_MAX_STALE_SECONDS'
]
var targetSecretNames = [
  'app-session-secret-key'
  'entra-client-secret'
]
var web = first(filter(snapshot.properties.template.containers, container => container.name == 'web'))
var retainedSecrets = filter(snapshot.properties.configuration.secrets, secret => !contains(targetSecretNames, secret.name))
var transferredSecrets = concat(retainedSecrets, [
  {
    name: 'app-session-secret-key'
    value: appSessionSecretKey
  }
  {
    name: 'entra-client-secret'
    value: entraClientSecret
  }
])
var staticSecrets = phase == 'transfer' ? transferredSecrets : snapshot.properties.configuration.secrets
var baseEnvironment = filter(web.env, setting => setting.name != 'APP_SESSION_SECRET_KEY' && setting.name != 'ENTRA_CLIENT_SECRET')
var staticEnvironment = concat(baseEnvironment, [
  {
    name: 'APP_SESSION_SECRET_KEY'
    secretRef: 'app-session-secret-key'
  }
  {
    name: 'ENTRA_CLIENT_SECRET'
    secretRef: 'entra-client-secret'
  }
])
var cleanupEnvironment = filter(staticEnvironment, setting => !contains(rotationOnlyEnvironmentNames, setting.name))
var transformedWeb = union(web, {
  image: phase == 'deploy-image' ? rollbackImage : web.image
  env: phase == 'cleanup-app' ? cleanupEnvironment : staticEnvironment
})
var transformedContainers = map(snapshot.properties.template.containers, container => container.name == 'web' ? transformedWeb : container)

resource app 'Microsoft.App/containerApps@2025-01-01' = {
  name: 'fcag-dev-app'
  location: snapshot.location
  tags: snapshot.tags
  identity: snapshot.identity
  properties: union(snapshot.properties, {
    configuration: union(snapshot.properties.configuration, {
      secrets: staticSecrets
    })
    template: union(snapshot.properties.template, {
      containers: transformedContainers
    })
  })
}
