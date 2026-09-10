// A separate deployment lets ARM validate existing-auth input before updating ACA.

@minLength(1)
@description('Microsoft Entra ID application (client) ID of an existing app registration. Must be non-empty when entraAuthMode is "existing". Bicep compiles this to an ARM minLength:1 constraint validated before any ACA resource is mutated.')
param entraClientIdOverride string

@description('The validated client ID, passed through to the caller to establish an implicit ARM dependency on this guard completing successfully.')
output validatedClientId string = entraClientIdOverride
