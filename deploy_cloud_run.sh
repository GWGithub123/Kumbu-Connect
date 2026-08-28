#!/usr/bin/env bash
set -euo pipefail

KUMBU_CLOUDSDK_CONFIG="${KUMBU_CLOUDSDK_CONFIG:-$HOME/.config/gcloud-kumbu}"

if [[ -z "${CLOUDSDK_CONFIG:-}" && -d "$KUMBU_CLOUDSDK_CONFIG" ]]; then
  export CLOUDSDK_CONFIG="$KUMBU_CLOUDSDK_CONFIG"
fi

CONFIG_NAME="${CLOUDSDK_ACTIVE_CONFIG_NAME:-kumbuconnect1}"
PROJECT_ID="${PROJECT_ID:-kumbuconnect1}"
REGION="${REGION:-us-east1}"
SERVICE_NAME="${SERVICE_NAME:-kumbu-connect-web}"
FIREBASE_SECRET_MOUNT="${FIREBASE_SECRET_MOUNT:-/secrets/firebase/firebase-service-account.json}"
GOOGLE_CLIENT_SECRET_MOUNT="${GOOGLE_CLIENT_SECRET_MOUNT:-/secrets/google/client-secret.json}"
GOOGLE_TOKEN_SECRET_MOUNT="${GOOGLE_TOKEN_SECRET_MOUNT:-/secrets/google-token/token.json}"
RUNTIME_SERVICE_ACCOUNT="${RUNTIME_SERVICE_ACCOUNT:-kumbu-cloud-run@${PROJECT_ID}.iam.gserviceaccount.com}"
CLOUDSQL_INSTANCE="${CLOUDSQL_INSTANCE:-${PROJECT_ID}:us-east1:kumbu-postgres}"
FIREBASE_STORAGE_BUCKET="${FIREBASE_STORAGE_BUCKET:-}"
PUBLIC_BASE_URL="${PUBLIC_BASE_URL:-}"
TWILIO_VALIDATE_SIGNATURE="${TWILIO_VALIDATE_SIGNATURE:-true}"
MEMORY="${MEMORY:-2Gi}"
CPU="${CPU:-1}"
CONCURRENCY="${CONCURRENCY:-1}"
TIMEOUT="${TIMEOUT:-900}"
SKIP_GCLOUD_CONFIG_CHECK="${SKIP_GCLOUD_CONFIG_CHECK:-false}"

gcloud_config_exists() {
  gcloud config configurations describe "$1" >/dev/null 2>&1
}

if gcloud_config_exists "$CONFIG_NAME"; then
  export CLOUDSDK_ACTIVE_CONFIG_NAME="$CONFIG_NAME"
  USING_GCLOUD_CONFIG=true
elif [[ "$SKIP_GCLOUD_CONFIG_CHECK" == "true" ]]; then
  USING_GCLOUD_CONFIG=false
else
  echo "gcloud config '$CONFIG_NAME' was not found. Set SKIP_GCLOUD_CONFIG_CHECK=true for CI or choose an existing config." >&2
  exit 1
fi

read_env_file_value() {
  local key="$1"
  python3 - "$key" <<'PY'
import os
import pathlib
import sys

key = sys.argv[1]
value = os.environ.get(key, '')
env_file = pathlib.Path('.env')
if not value and env_file.exists():
    for raw_line in env_file.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        candidate_key, candidate_value = line.split('=', 1)
        if candidate_key == key:
            value = candidate_value.strip()
            break
print(value)
PY
}

read_service_env_value() {
  local key="$1"
  gcloud run services describe "$SERVICE_NAME" \
    --project="$PROJECT_ID" \
    --region="$REGION" \
    --format=json 2>/dev/null | python3 - "$key" <<'PY'
import json
import sys

key = sys.argv[1]

try:
    payload = json.load(sys.stdin)
except Exception:
    print("")
    raise SystemExit

container_groups = [
    ((((payload.get("spec") or {}).get("template") or {}).get("spec") or {}).get("containers") or []),
    ((((payload.get("template") or {}).get("containers")) or [])),
]

for containers in container_groups:
    if not containers:
        continue
    for env_var in containers[0].get("env") or []:
        if env_var.get("name") == key and "value" in env_var:
            print(env_var.get("value") or "")
            raise SystemExit

print("")
PY
}

resolve_env_value() {
  local env_key="$1"
  local dotenv_key="${2:-$1}"
  local value="${!env_key:-}"

  if [[ -z "$value" ]]; then
    value="$(read_env_file_value "$dotenv_key")"
  fi

  if [[ -z "$value" ]]; then
    value="$(read_service_env_value "$env_key")"
  fi

  printf '%s' "$value"
}

secret_exists() {
  local secret_name="$1"
  gcloud secrets describe "$secret_name" \
    --project="$PROJECT_ID" \
    >/dev/null 2>&1
}

append_env_var() {
  local key="$1"
  local value="$2"
  ENV_VARS+=("${key}=${value}")
}

write_env_vars_file() {
  local output_path="$1"
  python3 - "$output_path" "${ENV_VARS[@]}" <<'PY'
import json
import pathlib
import sys

output_path = pathlib.Path(sys.argv[1])
entries = sys.argv[2:]
with output_path.open('w', encoding='utf-8') as handle:
    for entry in entries:
        key, value = entry.split('=', 1)
        handle.write(f'{key}: {json.dumps(value)}\n')
PY
}

if [[ "$USING_GCLOUD_CONFIG" == "true" ]]; then
  echo "Using gcloud config: $CONFIG_NAME"
else
  echo "Using current gcloud auth context"
fi
if [[ -n "${CLOUDSDK_CONFIG:-}" ]]; then
  echo "Using Cloud SDK config dir: $CLOUDSDK_CONFIG"
fi
echo "Using project: $PROJECT_ID"
echo "Using region: $REGION"
echo "Deploying service: $SERVICE_NAME"

if [[ -z "$PUBLIC_BASE_URL" ]]; then
  PUBLIC_BASE_URL="$(gcloud run services describe "$SERVICE_NAME" \
    --project="$PROJECT_ID" \
    --region="$REGION" \
    --format='value(status.url)' 2>/dev/null || true)"
fi

if [[ -z "$PUBLIC_BASE_URL" ]]; then
  PROJECT_NUMBER="${PROJECT_NUMBER:-$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')}"
  PUBLIC_BASE_URL="https://${SERVICE_NAME}-${PROJECT_NUMBER}.${REGION}.run.app"
fi

GOOGLE_SEARCH_ENGINE_ID="$(resolve_env_value GOOGLE_SEARCH_ENGINE_ID GOOGLE_SEARCH_ENGINE_ID)"
AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT="$(resolve_env_value AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT Azure_Document_Intelligence_Endpoint)"
GOOGLE_DEVELOPER_ALLOWED_EMAILS="$(resolve_env_value GOOGLE_DEVELOPER_ALLOWED_EMAILS GOOGLE_DEVELOPER_ALLOWED_EMAILS)"
GOOGLE_DEVELOPER_EXISTING_USER_BYPASS_EMAILS="$(resolve_env_value GOOGLE_DEVELOPER_EXISTING_USER_BYPASS_EMAILS GOOGLE_DEVELOPER_EXISTING_USER_BYPASS_EMAILS)"
FIREBASE_STORAGE_BUCKET="${FIREBASE_STORAGE_BUCKET:-$(resolve_env_value FIREBASE_STORAGE_BUCKET FIREBASE_STORAGE_BUCKET)}"
DEPLOY_TEMP_LOGIN_BYPASS_ENABLED="${DEPLOY_TEMP_LOGIN_BYPASS_ENABLED:-false}"
DEPLOY_TEMP_LOGIN_BYPASS_CBO_SLUG="${DEPLOY_TEMP_LOGIN_BYPASS_CBO_SLUG:-}"

if [[ -z "$FIREBASE_STORAGE_BUCKET" ]]; then
  echo "FIREBASE_STORAGE_BUCKET is required. Use the exact bucket name shown in Firebase Storage, for example kumbuconnect1-318a0.firebasestorage.app." >&2
  exit 1
fi

ENV_VARS=()
append_env_var FIREBASE_PROJECT_ID "$PROJECT_ID"
append_env_var FIREBASE_STORAGE_BUCKET "$FIREBASE_STORAGE_BUCKET"
append_env_var PUBLIC_BASE_URL "$PUBLIC_BASE_URL"
append_env_var TRUST_PROXY_HEADERS true
append_env_var PREFERRED_URL_SCHEME https
append_env_var SESSION_COOKIE_SECURE true
append_env_var SESSION_COOKIE_SAMESITE Lax
append_env_var REMEMBER_COOKIE_SAMESITE Lax
append_env_var ALLOW_LOCAL_FILE_STORAGE_FALLBACK false
append_env_var ALLOW_GOOGLE_ADC_FALLBACK false
append_env_var TWILIO_VALIDATE_SIGNATURE "$TWILIO_VALIDATE_SIGNATURE"
append_env_var FIREBASE_SERVICE_ACCOUNT_JSON "$FIREBASE_SECRET_MOUNT"

if secret_exists kumbu-google-oauth-client-secret-json; then
  append_env_var GOOGLE_OAUTH_CLIENT_SECRET_JSON "$GOOGLE_CLIENT_SECRET_MOUNT"
  append_env_var GOOGLE_DEVELOPER_CLIENT_SECRET_JSON "$GOOGLE_CLIENT_SECRET_MOUNT"
  append_env_var GOOGLE_USER_CLIENT_SECRET_JSON "$GOOGLE_CLIENT_SECRET_MOUNT"
fi

if secret_exists kumbu-google-oauth-token-json; then
  append_env_var GOOGLE_OAUTH_TOKEN_JSON "$GOOGLE_TOKEN_SECRET_MOUNT"
  append_env_var GOOGLE_AUTHORIZED_USER_JSON "$GOOGLE_TOKEN_SECRET_MOUNT"
fi

if [[ -n "$GOOGLE_SEARCH_ENGINE_ID" ]]; then
  append_env_var GOOGLE_SEARCH_ENGINE_ID "$GOOGLE_SEARCH_ENGINE_ID"
fi
if [[ -n "$AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT" ]]; then
  append_env_var AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT "$AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT"
fi
if [[ -n "$GOOGLE_DEVELOPER_ALLOWED_EMAILS" ]]; then
  append_env_var GOOGLE_DEVELOPER_ALLOWED_EMAILS "$GOOGLE_DEVELOPER_ALLOWED_EMAILS"
fi
if [[ -n "$GOOGLE_DEVELOPER_EXISTING_USER_BYPASS_EMAILS" ]]; then
  append_env_var GOOGLE_DEVELOPER_EXISTING_USER_BYPASS_EMAILS "$GOOGLE_DEVELOPER_EXISTING_USER_BYPASS_EMAILS"
fi
append_env_var TEMP_LOGIN_BYPASS_ENABLED "$DEPLOY_TEMP_LOGIN_BYPASS_ENABLED"
if [[ -n "$DEPLOY_TEMP_LOGIN_BYPASS_CBO_SLUG" ]]; then
  append_env_var TEMP_LOGIN_BYPASS_CBO_SLUG "$DEPLOY_TEMP_LOGIN_BYPASS_CBO_SLUG"
fi

ENV_VARS_FILE="$(mktemp -t kumbu-run-env)"
trap 'rm -f "$ENV_VARS_FILE"' EXIT
write_env_vars_file "$ENV_VARS_FILE"

SECRET_MAPPINGS=(
  "${FIREBASE_SECRET_MOUNT}=kumbu-firebase-service-account-json:latest"
  "SECRET_KEY=kumbu-secret-key:latest"
  "DATABASE_URL=kumbu-database-url:latest"
  "Kobo_Toobox_API_Key=kumbu-kobo-api-key:latest"
  "Gemini_API_Key=kumbu-gemini-api-key:latest"
  "OPENAI_API_KEY=kumbu-openai-api-key:latest"
  "CLAUDE_API_KEY=kumbu-claude-api-key:latest"
  "AZURE_DOCUMENT_INTELLIGENCE_KEY=kumbu-azure-document-intelligence-key:latest"
  "GOOGLE_MAPS_API_KEY=kumbu-google-maps-api-key:latest"
  "GOOGLE_SEARCH_API_KEY=kumbu-google-search-api-key:latest"
  "TWILIO_ACCOUNT_SID=kumbu-twilio-account-sid:latest"
  "TWILIO_AUTH_TOKEN=kumbu-twilio-auth-token:latest"
  "TWILIO_PHONE_NUMBER=kumbu-twilio-phone-number:latest"
)

if secret_exists kumbu-google-oauth-client-secret-json; then
  SECRET_MAPPINGS+=("${GOOGLE_CLIENT_SECRET_MOUNT}=kumbu-google-oauth-client-secret-json:latest")
fi

if secret_exists kumbu-google-oauth-token-json; then
  SECRET_MAPPINGS+=("${GOOGLE_TOKEN_SECRET_MOUNT}=kumbu-google-oauth-token-json:latest")
fi

SECRET_MAPPINGS_ARG="$(printf '%s,' "${SECRET_MAPPINGS[@]}")"
SECRET_MAPPINGS_ARG="${SECRET_MAPPINGS_ARG%,}"

exec gcloud run deploy "$SERVICE_NAME" \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --source . \
  --service-account="$RUNTIME_SERVICE_ACCOUNT" \
  --add-cloudsql-instances="$CLOUDSQL_INSTANCE" \
  --allow-unauthenticated \
  --memory="$MEMORY" \
  --cpu="$CPU" \
  --concurrency="$CONCURRENCY" \
  --timeout="$TIMEOUT" \
  --env-vars-file="$ENV_VARS_FILE" \
  --set-secrets="$SECRET_MAPPINGS_ARG" \
  --quiet \
  "$@"
