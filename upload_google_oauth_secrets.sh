#!/usr/bin/env bash
set -euo pipefail

CLIENT_SECRET_FILE="${1:-}"
TOKEN_FILE="${2:-}"
PROJECT_ID="${PROJECT_ID:-kumbuconnect1}"
RUNTIME_SERVICE_ACCOUNT="${RUNTIME_SERVICE_ACCOUNT:-kumbu-cloud-run@${PROJECT_ID}.iam.gserviceaccount.com}"

if [[ -z "$CLIENT_SECRET_FILE" || -z "$TOKEN_FILE" ]]; then
  echo "Usage: $0 /path/to/oauth-client-secret.json /path/to/google-forms-token.json" >&2
  exit 1
fi

for FILE_PATH in "$CLIENT_SECRET_FILE" "$TOKEN_FILE"; do
  if [[ ! -f "$FILE_PATH" ]]; then
    echo "File not found: $FILE_PATH" >&2
    exit 1
  fi
done

create_or_update_secret() {
  local secret_name="$1"
  local file_path="$2"

  if ! gcloud secrets describe "$secret_name" --project "$PROJECT_ID" >/dev/null 2>&1; then
    gcloud secrets create "$secret_name" --project "$PROJECT_ID" --replication-policy=automatic >/dev/null
  fi

  gcloud secrets versions add "$secret_name" --project "$PROJECT_ID" --data-file "$file_path" >/dev/null
  gcloud secrets add-iam-policy-binding "$secret_name" \
    --project "$PROJECT_ID" \
    --member "serviceAccount:${RUNTIME_SERVICE_ACCOUNT}" \
    --role roles/secretmanager.secretAccessor >/dev/null 2>&1 || true
}

create_or_update_secret kumbu-google-oauth-client-secret-json "$CLIENT_SECRET_FILE"
create_or_update_secret kumbu-google-oauth-token-json "$TOKEN_FILE"

printf 'Uploaded fresh Google OAuth secrets to project %s\n' "$PROJECT_ID"
printf '  client secret: kumbu-google-oauth-client-secret-json\n'
printf '  forms token:   kumbu-google-oauth-token-json\n'
printf 'Next step: redeploy with ./deploy_cloud_run.sh\n'