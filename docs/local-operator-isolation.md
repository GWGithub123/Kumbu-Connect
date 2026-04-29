# Local Operator Isolation

This workstation now separates Kumbu and Houseyield Cloud SDK state.

## Cloud SDK Homes

- `~/.config/gcloud-kumbu`: Kumbu-only Cloud SDK home
- `~/.config/gcloud-houseyield`: Houseyield-only Cloud SDK home
- `~/.config/gcloud`: no longer trusted as an operational home for either project

The old shared `default` configuration was neutralized so unscoped `gcloud` commands do not keep targeting Houseyield by accident.

## Kumbu Commands

Use the repo wrapper whenever you run manual Kumbu infrastructure commands:

```bash
./kgcloud config list
./kgcloud run services describe kumbu-connect-web --region=us-east1
```

`./kgcloud` prefers `~/.config/gcloud-kumbu` automatically.

## Houseyield Commands

Houseyield no longer lives in the shared active `default` config. Use an explicit Cloud SDK home when you need Houseyield access:

```bash
CLOUDSDK_CONFIG="$HOME/.config/gcloud-houseyield" gcloud config list
```

If you want a shell alias outside the repo, add one in your shell profile rather than reusing the shared `default` config.

## Browser Separation

Cloud SDK separation does not isolate browser sessions.

For Kumbu browser access, keep it in a dedicated browser profile that is signed in only with Kumbu identities. On Safari, profile creation is still a manual UI step. Recommended setup:

1. Create a Safari profile named `Kumbu Connect`.
2. Sign in only with the Kumbu-owned Google account(s) in that profile.
3. Use that profile for Cloud Console, Google OAuth consent/admin work, and any Kumbu Gmail access.
4. Keep Houseyield in a different Safari profile.

This is separate from the Cloud SDK split above. You want both.

## Current Limitation

The isolated Kumbu Cloud SDK home contains only Kumbu account entries, but the currently configured Kumbu service-account credentials are stale and need one fresh Kumbu re-auth or service-account activation before live Kumbu admin commands will work from the isolated home again.