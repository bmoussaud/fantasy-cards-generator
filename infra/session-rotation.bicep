@allowed(['dev'])
param environmentName string = 'dev'

param enableSessionRotation bool = false
param sessionRunId string
param sessionExpectedHash string
param existingIdentityPrincipalId string = ''

module session './modules/session-rotation-runner.bicep' = if (enableSessionRotation) {
  name: 'session-rotation-runner'
  params: {
    environmentName: environmentName
    sessionRunId: sessionRunId
    sessionExpectedHash: sessionExpectedHash
    existingIdentityPrincipalId: existingIdentityPrincipalId
  }
}
