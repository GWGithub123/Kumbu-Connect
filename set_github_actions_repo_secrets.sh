#!/usr/bin/env bash
set -euo pipefail

REPO="${GH_REPO:-${1:-}}"

if [[ -z "$REPO" ]]; then
  ORIGIN_URL="$(git remote get-url origin 2>/dev/null || true)"
  case "$ORIGIN_URL" in
    git@github.com:*)
      REPO="${ORIGIN_URL#git@github.com:}"
      REPO="${REPO%.git}"
      ;;
    git@github-kumbu:*)
      REPO="${ORIGIN_URL#git@github-kumbu:}"
      REPO="${REPO%.git}"
      ;;
    https://github.com/*)
      REPO="${ORIGIN_URL#https://github.com/}"
      REPO="${REPO%.git}"
      ;;
  esac
fi

if [[ -z "$REPO" ]]; then
  echo "Could not determine the GitHub repository. Pass owner/repo as the first argument or set GH_REPO." >&2
  exit 1
fi

if ! command -v gh >/dev/null 2>&1; then
  echo "GitHub CLI is not installed." >&2
  exit 1
fi

if ! gh auth status >/dev/null 2>&1; then
  echo "Run 'gh auth login' first, then rerun this script." >&2
  exit 1
fi

GCP_WORKLOAD_IDENTITY_PROVIDER="${GCP_WORKLOAD_IDENTITY_PROVIDER:-projects/62809838048/locations/global/workloadIdentityPools/github-actions/providers/kumbu-connect-github}"
GCP_DEPLOY_SERVICE_ACCOUNT="${GCP_DEPLOY_SERVICE_ACCOUNT:-github-actions-deployer@kumbuconnect1.iam.gserviceaccount.com}"

gh secret set GCP_WORKLOAD_IDENTITY_PROVIDER --repo "$REPO" --body "$GCP_WORKLOAD_IDENTITY_PROVIDER"
gh secret set GCP_DEPLOY_SERVICE_ACCOUNT --repo "$REPO" --body "$GCP_DEPLOY_SERVICE_ACCOUNT"

printf 'Updated GitHub Actions secrets for %s\n' "$REPO"
printf '  GCP_WORKLOAD_IDENTITY_PROVIDER=%s\n' "$GCP_WORKLOAD_IDENTITY_PROVIDER"
printf '  GCP_DEPLOY_SERVICE_ACCOUNT=%s\n' "$GCP_DEPLOY_SERVICE_ACCOUNT"