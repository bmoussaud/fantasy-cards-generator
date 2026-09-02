@description('Deployment location for security resources.')
param location string

@description('Key Vault resource name. Must be globally unique.')
param keyVaultName string

@description('Principal ID of the managed identity granted read access to Key Vault secrets (the Container App\'s ACR-pull identity).')
param keyVaultAccessPrincipalId string

@description('Microsoft Entra object ID of the azd/ARM deployment caller that needs metadata-only browse access to Key Vault objects.')
param deployerPrincipalId string

@allowed([
  'ServicePrincipal'
  'User'
])
@description('Microsoft Entra principal type for the azd/ARM deployment caller.')
param deployerPrincipalType string

@secure()
@description('Session cookie signing secret stored in Key Vault as APP_SESSION_SECRET_KEY. Empty skips creating the secret.')
param appSessionSecretKeyValue string = ''

@secure()
@description('Microsoft Entra ID client secret stored in Key Vault as ENTRA_CLIENT_SECRET. Empty skips creating the secret.')
param entraClientSecretValue string = ''

@description('Optional tags shared by security resources.')
param tags object = {}

var keyVaultSecretsUserRoleDefinitionId = '4633458b-17de-408a-b874-0445c86b69e6'
var keyVaultReaderRoleDefinitionId = '21090545-7ca7-4776-b22c-e363652d74d2'
var hasAppSessionSecretKeyValue = !empty(appSessionSecretKeyValue)
var hasEntraClientSecretValue = !empty(entraClientSecretValue)

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: keyVaultName
  location: location
  tags: tags
  properties: {
    enablePurgeProtection: true
    enableRbacAuthorization: true
    networkAcls: {
      bypass: 'AzureServices'
      defaultAction: 'Allow'
    }
    publicNetworkAccess: 'Enabled'
    sku: {
      family: 'A'
      name: 'standard'
    }
    tenantId: tenant().tenantId
  }
}

resource keyVaultSecretsUserRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVault.id, keyVaultAccessPrincipalId, keyVaultSecretsUserRoleDefinitionId)
  scope: keyVault
  properties: {
    principalId: keyVaultAccessPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', keyVaultSecretsUserRoleDefinitionId)
  }
}

resource deployerKeyVaultReaderRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVault.id, deployerPrincipalId, keyVaultReaderRoleDefinitionId)
  scope: keyVault
  properties: {
    principalId: deployerPrincipalId
    principalType: deployerPrincipalType
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', keyVaultReaderRoleDefinitionId)
  }
}

resource appSessionSecretKeySecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = if (hasAppSessionSecretKeyValue) {
  parent: keyVault
  name: 'app-session-secret-key'
  properties: {
    value: appSessionSecretKeyValue
  }
}

resource entraClientSecretSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = if (hasEntraClientSecretValue) {
  parent: keyVault
  name: 'entra-client-secret'
  properties: {
    value: entraClientSecretValue
  }
}

output keyVaultName string = keyVault.name
output keyVaultUri string = keyVault.properties.vaultUri
output appSessionSecretKeySecretUri string = hasAppSessionSecretKeyValue ? appSessionSecretKeySecret!.properties.secretUri : ''
output entraClientSecretSecretUri string = hasEntraClientSecretValue ? entraClientSecretSecret!.properties.secretUri : ''
