# Kumbu Secret Audit

Date: 2026-04-26

This audit covers the secret names currently referenced by [deploy_cloud_run.sh](../deploy_cloud_run.sh). It is a provenance audit based on repository configuration and previously verified deployment notes. It is not a live Secret Manager payload dump.

## Current Secret References

The Cloud Run deploy path references these Secret Manager entries:

- `kumbu-firebase-service-account-json`
- `kumbu-google-oauth-client-secret-json`
- `kumbu-google-oauth-token-json`
- `kumbu-secret-key`
- `kumbu-database-url`
- `kumbu-kobo-api-key`
- `kumbu-gemini-api-key`
- `kumbu-openai-api-key`
- `kumbu-claude-api-key`
- `kumbu-azure-document-intelligence-key`
- `kumbu-google-maps-api-key`
- `kumbu-google-search-api-key`
- `kumbu-twilio-account-sid`
- `kumbu-twilio-auth-token`
- `kumbu-twilio-phone-number`

## Known External-Origin Item

- `kumbu-google-oauth-client-secret-json`

Known provenance:

- This secret was previously rotated to a Google OAuth web client that belongs to Google project `project-7633607d-5423-4112-af9`, not to the `kumbu-connect` GCP project.
- This is documented in repository memory and was already verified during production login repair work.

Risk:

- The website hosting remains in `kumbu-connect`, but OAuth ownership is still coupled to an external Google project.
- Whoever controls that external Google project controls the lifecycle of that OAuth client.

Recommended action:

1. Create a Kumbu-owned OAuth web client in the `kumbu-connect` Google Cloud project.
2. Rotate `kumbu-google-oauth-client-secret-json` to that Kumbu-owned client.
3. Reauthorize the allowed redirect URIs on the Kumbu-owned client only.

## Likely External or User-Bound Item

- `kumbu-google-oauth-token-json`

Known provenance:

- This is a user-authorized token file mounted for Google integrations.
- The exact Google account and OAuth project backing the current live token were not revalidated in this audit because current local Kumbu admin credentials are stale.

Risk:

- User-authorized tokens can tie production behavior to a personal or legacy Google account.

Recommended action:

1. Reissue this token under a Kumbu-owned Google identity.
2. Record which Google account owns it and which OAuth project issued it.

## Items With Unverified Provenance

These secret names are expected to be Kumbu-owned, but their true origin was not revalidated in this audit:

- `kumbu-firebase-service-account-json`
- `kumbu-secret-key`
- `kumbu-database-url`
- `kumbu-kobo-api-key`
- `kumbu-gemini-api-key`
- `kumbu-openai-api-key`
- `kumbu-claude-api-key`
- `kumbu-azure-document-intelligence-key`
- `kumbu-google-maps-api-key`
- `kumbu-google-search-api-key`
- `kumbu-twilio-account-sid`
- `kumbu-twilio-auth-token`
- `kumbu-twilio-phone-number`

Current status:

- No repository evidence currently ties these secrets to Houseyield.
- No live Secret Manager metadata or payload verification was possible in this audit because local Kumbu admin credentials need a fresh re-auth before direct Secret Manager reads can succeed again.

## Why The Audit Is Incomplete

The isolated Kumbu Cloud SDK home can load the Kumbu configuration, but the currently stored Kumbu credentials are stale for live API access. A fresh Kumbu re-auth is required before a live Secret Manager audit can confirm payload provenance, labels, version history, and access bindings.

## Next Live Audit Steps

1. Re-authenticate a Kumbu-owned admin identity into `~/.config/gcloud-kumbu`.
2. Run `./kgcloud secrets list --project=kumbu-connect`.
3. For each secret above, inspect labels, versions, and last-updated metadata.
4. Rotate any secret that came from a non-Kumbu project, non-Kumbu account, or shared workstation export.