extension graphBeta

@description('Unique app registration name and display name.')
param appName string

@description('Short description for the Entra ID app registration.')
param appDescription string = 'Microsoft Entra ID app registration for the fantasy-cards-generator web application.'

@description('Redirect URIs that must exactly match the web app OIDC callback endpoints.')
param redirectUris array

@description('Tenant ID where the application is registered.')
param tenantId string = tenant().tenantId

resource application 'Microsoft.Graph/applications@beta' = {
  uniqueName: appName
  description: appDescription
  displayName: appName
  signInAudience: 'AzureADMultipleOrgs'
  owners: {
    relationships: [deployer().objectId]
  }
  web: {
    redirectUris: redirectUris
    implicitGrantSettings: {
      enableIdTokenIssuance: false
      enableAccessTokenIssuance: false
    }
  }
}

resource servicePrincipal 'Microsoft.Graph/servicePrincipals@beta' = {
  appId: application.appId
  accountEnabled: true
  servicePrincipalType: 'Application'
  owners: {
    relationships: [deployer().objectId]
  }
}

// Microsoft Graph rejects declarative passwordCredentials for this resource type.
// Create ENTRA_CLIENT_SECRET separately (for example: az ad app credential reset --id <appId>)
// and store the resulting secret in Key Vault or another secure secret store.
output appId string = application.appId
output appObjectId string = application.id
output servicePrincipalId string = servicePrincipal.id
output tenantId string = tenantId
