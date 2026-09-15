# Provisioning the Paul and Jane Smith workforce users

The repository photo is `pics/paul_smith.png`. User provisioning is a
one-time manual operation performed by a tenant administrator.

## Why a separate script is used

The Microsoft Graph Bicep extension does not provide a supported write path for
users or profile-photo streams. Azure `Microsoft.Resources/deploymentScripts`
also cannot run in this subscription because its backing storage requires
shared-key authentication, which policy blocks. The script uses the Azure CLI
authentication of the administrator running it. User creation uses `az ad`;
profile-photo upload uses `curl --data-binary` because the Azure CLI REST
wrapper does not reliably preserve the binary JPEG request body.

`pics/paul_smith.jpg` and `pics/jane_smith.jpg` are checked-in, 512px RGB
baseline JPEG derivatives used by the script. They are generated from
`pics/paul_smith.png` and `pics/jane_smith.png`; the PNG files remain the source
assets. Microsoft Graph requires the photo request body to contain binary JPEG
data and may reject progressive JPEGs as `InvalidImage`.

## Prerequisites

- Install the Azure CLI and `curl`. If your `User Administrator` role is eligible through
  Microsoft Entra Privileged Identity Management (PIM), activate it before
  running the script. In the [Microsoft Entra admin
  center](https://entra.microsoft.com), open **ID Governance** >
  **Privileged Identity Management** > **My roles** > **Microsoft Entra
  roles**, select **User Administrator**, and choose **Activate**. Complete
  the required MFA/verification, provide an activation reason, and wait for
  the activation to complete. See Microsoft's
  [PIM role activation
  guide](https://learn.microsoft.com/en-us/entra/id-governance/privileged-identity-management/pim-how-to-activate-role).
- Authenticate the Azure CLI after activating the role:

  ```bash
  az login --tenant '<workforce-tenant-id>'
  ```

- From the repository root, create or select the `dev` azd environment. The
  script reads `AZURE_ENV_NAME` and the initial password from that azd
  environment:

  ```bash
  azd env new dev
  azd env set AZURE_ENV_NAME dev
  azd env set AZURE_SUBSCRIPTION_ID '<subscription-id>'
  azd env set AZURE_LOCATION '<azure-region>'
  ```

- The administrator used by `az` must be allowed to create users in the
  workforce tenant. Assign the `User Administrator` directory role and grant
  delegated Microsoft Graph `User.ReadWrite.All` consent. Existing users are
  only looked up and are never sent a password update.
- Set `ENTRA_USER_INITIAL_PASSWORD` only when creating a user. It is passed to
  the Azure CLI process and is never printed or returned.

## Run once as tenant administrator

```bash
# Required only for first-time user creation; keep this value out of source control.
azd env set ENTRA_USER_INITIAL_PASSWORD '<temporary-password>'

./hooks/create_entra_users.sh
```

The script discovers the default verified tenant domain and reconciles:

- `Paul Smith` at `paul.smith@<default-verified-domain>`
- `Jane Smith` at `jane.smith@<default-verified-domain>`

It applies `pics/paul_smith.jpg` to Paul Smith and `pics/jane_smith.jpg` to
Jane Smith. Existing users are not recreated and their passwords are not reset.

If the `User Administrator` role was activated through PIM, run the script
before the activation expires. If Azure CLI was already logged in before PIM
activation, sign in again with `az login` so the CLI obtains a token reflecting
the active role.

Do not edit `.azure/<environment>/.env` to add the password. Use `azd env set`
so the value remains in the local azd environment and is not documented or
committed.

No password is stored in Bicep, ARM JSON, or source control. Set
`ENTRA_USER_INITIAL_PASSWORD` as a local azd environment value before the first
provisioning. Existing users are reconciled without resetting credentials.

The script creates missing users with the supplied initial password and forced
first-sign-in change. Failures report sanitized operation-level messages and
never print the password or access token.

Run the script again after correcting an error. It is idempotent and does not
reset existing users' passwords.
