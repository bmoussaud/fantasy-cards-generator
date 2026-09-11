@allowed(['dev'])
param environmentName string = 'dev'

@description('Explicit opt-in. Incremental false deployment does not delete an existing runner.')
param enablePrivateMetadataRunner bool = false

module runner './modules/private-metadata-runner.bicep' = if (enablePrivateMetadataRunner) {
  name: 'private-metadata-runner'
  params: {
    environmentName: environmentName
  }
}
