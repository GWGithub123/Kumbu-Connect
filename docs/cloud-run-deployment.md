# Cloud Run Deployment

This project can be deployed to Cloud Run without touching your machine-wide default `gcloud` config.

The repo tooling now prefers a dedicated Cloud SDK home at `~/.config/gcloud-kumbu` when that directory exists. This keeps Kumbu auth and cached tokens out of the shared `~/.config/gcloud` home that may also contain unrelated project access.

## 1. Keep Kumbu On Its Own Gcloud Config

Use the dedicated config every time you work on Kumbu infrastructure:

```bash
CLOUDSDK_CONFIG="$HOME/.config/gcloud-kumbu" \
CLOUDSDK_ACTIVE_CONFIG_NAME=kumbuconnect1 gcloud config list
```

The repo helper script [deploy_cloud_run.sh](../deploy_cloud_run.sh) now prefers `~/.config/gcloud-kumbu` automatically.
The repo wrapper [kgcloud](../kgcloud) does the same for manual `gcloud` commands.

## 2. Use The Correct Admin Login

The runtime Firebase service account can access the `kumbuconnect1` project, but it may not have enough permissions to enable services, create Cloud SQL instances, or deploy Cloud Run revisions.

For provisioning, use a human Google account with the needed IAM roles in the Kumbu project:

```bash
gcloud config configurations activate kumbuconnect1
gcloud auth login
gcloud auth application-default login
gcloud config set project kumbuconnect1
gcloud config set run/region us-east1
```

If you want to avoid changing the globally active config, run the same commands with both `CLOUDSDK_CONFIG="$HOME/.config/gcloud-kumbu"` and `CLOUDSDK_ACTIVE_CONFIG_NAME=kumbuconnect1` prefixed instead.

## 3. Enable Required Services

```bash
CLOUDSDK_CONFIG="$HOME/.config/gcloud-kumbu" \
CLOUDSDK_ACTIVE_CONFIG_NAME=kumbuconnect1 gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  sqladmin.googleapis.com \
  secretmanager.googleapis.com
```

Firebase and Firestore are already relevant to this app. For stateless hosting, make sure Firebase Storage is provisioned and `FIREBASE_STORAGE_BUCKET` is set to the exact bucket name shown in Firebase Storage. Do not assume it matches the project ID exactly; newer projects often get names like `kumbuconnect1-318a0.firebasestorage.app`.

## 4. Provision The Production Database

This app now accepts `DATABASE_URL`. For Cloud SQL Postgres, provision a Postgres instance and database, then use a connection string such as:

```text
postgresql+psycopg://DB_USER:DB_PASSWORD@/DB_NAME?host=/cloudsql/INSTANCE_CONNECTION_NAME
```

Use that as the `DATABASE_URL` runtime setting on Cloud Run.

## 5. Required Production Runtime Settings

At minimum, set these on the Cloud Run service:

```text
DATABASE_URL=postgresql+psycopg://...
SECRET_KEY=...
TRUST_PROXY_HEADERS=true
PREFERRED_URL_SCHEME=https
SESSION_COOKIE_SECURE=true
ALLOW_LOCAL_FILE_STORAGE_FALLBACK=false
ALLOW_GOOGLE_ADC_FALLBACK=false
PUBLIC_BASE_URL=https://your-cloud-run-service-url
FIREBASE_PROJECT_ID=kumbuconnect1
FIREBASE_STORAGE_BUCKET=kumbuconnect1-318a0.firebasestorage.app
FIREBASE_SERVICE_ACCOUNT_JSON=/secrets/firebase/firebase-service-account.json
```

Then add the external-service secrets you actually use in production, such as Twilio, OpenAI, Claude, Google Maps, Google Search, Kobo, and Google OAuth credentials.

The deploy helper applies a few production-safe defaults automatically:

- `PUBLIC_BASE_URL` defaults to the Cloud Run `run.app` service URL for the target project and region unless you override it.
- `TWILIO_VALIDATE_SIGNATURE=true` so inbound SMS requests must be signed by Twilio.
- `TEMP_LOGIN_BYPASS_ENABLED=false` unless you explicitly opt into a demo deploy with `DEPLOY_TEMP_LOGIN_BYPASS_ENABLED=true`.

If you mount JSON secrets as files, keep the env vars pointed at those mounted file paths.
Cloud Run cannot mount different secrets into the exact same directory, so use separate directories such as:

```text
FIREBASE_SERVICE_ACCOUNT_JSON=/secrets/firebase/firebase-service-account.json
GOOGLE_OAUTH_CLIENT_SECRET_JSON=/secrets/google/client-secret.json
GOOGLE_OAUTH_TOKEN_JSON=/secrets/google-token/token.json
```

The deploy helper now treats the Google OAuth client-secret mount and the Google Forms token mount as optional. That means you can deploy the app before those two secrets exist, but Google sign-in will stay disabled until `kumbu-google-oauth-client-secret-json` is created, and Google Forms sync/file-download features will stay disabled until `kumbu-google-oauth-token-json` is created.

For a fresh `kumbuconnect1` Google setup, create one new Web application OAuth client and add these redirect URIs:

```text
https://kumbu-connect-web-62809838048.us-east1.run.app/developer-login/google/callback
https://kumbu-connect-web-62809838048.us-east1.run.app/login/google/callback
http://127.0.0.1:8000/developer-login/google/callback
http://127.0.0.1:8000/login/google/callback
http://127.0.0.1:8765/google/forms/oauth/callback
```

Then generate a fresh Google Forms token with the fixed local callback used by [../authorize_google_forms.py](../authorize_google_forms.py):

```bash
GOOGLE_OAUTH_CLIENT_SECRET_JSON=/absolute/path/to/client-secret.json \
GOOGLE_OAUTH_TOKEN_JSON=/tmp/kumbu-google-forms-token.json \
python authorize_google_forms.py
```

After that, upload both fresh files into Secret Manager with:

```bash
./upload_google_oauth_secrets.sh /absolute/path/to/client-secret.json /tmp/kumbu-google-forms-token.json
```

## 6. Deploy Without Touching The Default Gcloud Config

```bash
./deploy_cloud_run.sh
```

Or explicitly:

```bash
CLOUDSDK_CONFIG="$HOME/.config/gcloud-kumbu" \
CLOUDSDK_ACTIVE_CONFIG_NAME=kumbuconnect1 gcloud run deploy kumbu-connect-web \
  --project=kumbuconnect1 \
  --region=us-east1 \
  --source . \
  --allow-unauthenticated
```

## 8. Dedicated CI Deploy Path

Laptop-based deploys are still useful for emergency recovery, but the preferred path is now CI-based. Use the workflow in [../.github/workflows/deploy-cloud-run.yml](../.github/workflows/deploy-cloud-run.yml) together with the setup steps in [ci-deployment.md](ci-deployment.md).

That workflow avoids any dependency on your local browser sessions, local token cache, or your shared workstation `gcloud` home.

## 7. Post-Deploy Follow-Up

After the public URL exists, update:

- Google OAuth redirect URIs
- Twilio webhook URL for `/sms/webhook`
- Any authorized domains needed for Firebase/Google sign-in flows

If you need to import exported legacy bookkeeping files from the retired Firebase project into the new bucket without reusing legacy runtime credentials, use:

```bash
python migrate_legacy_bookkeeping_files.py \
  --source-root /absolute/path/to/exported/bookkeeping_uploads \
  --database-url "$DATABASE_URL" \
  --firebase-service-account-json /absolute/path/to/firebase-service-account.json \
  --firebase-bucket kumbuconnect1-318a0.firebasestorage.app
```

If you temporarily need the demo password bypass on a non-production deploy, run:

```bash
DEPLOY_TEMP_LOGIN_BYPASS_ENABLED=true \
DEPLOY_TEMP_LOGIN_BYPASS_CBO_SLUG=your-cbo-slug \
./deploy_cloud_run.sh
```

## Offline Bookkeeping After Hosting

The offline bookkeeping app should continue to work after public deployment.

It is implemented as a service-worker and IndexedDB-backed client under:

- `webapp/templates/bookkeeping_offline.html`
- `webapp/static/js/bookkeeping_offline_app.js`
- `webapp/static/js/bookkeeping_offline_sw.js`

In practice, public HTTPS hosting is better for this feature because service workers and background sync behave more reliably on secure origins than on ad hoc local URLs.