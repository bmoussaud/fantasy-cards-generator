@description('Deployment location for security resources.')
param location string

@description('Key Vault resource name. Must be globally unique.')
param keyVaultName string

@description('Optional tags shared by security resources.')
param tags object = {}

module keyVault 'br/public:avm/res/key-vault/vault:0.13.3' = {
  name: 'key-vault'
  params: {
    name: keyVaultName
    location: location
    enableRbacAuthorization: true
    enablePurgeProtection: true
    publicNetworkAccess: 'Enabled'
    sku: 'standard'
    tags: tags
  }
}

output keyVaultName string = keyVault.outputs.name
output keyVaultUri string = keyVault.outputs.uri
