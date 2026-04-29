# CI Deployment For Cloud Run

This repo now includes a GitHub Actions workflow at [../.github/workflows/deploy-cloud-run.yml](../.github/workflows/deploy-cloud-run.yml) so Cloud Run deploys can move off a mixed-use laptop.

## Recommended Model

Use GitHub Actions with Workload Identity Federation and a dedicated Kumbu deploy service account.

That keeps deploy authority in the `kumbu-connect` project and avoids long-lived JSON keys on developer laptops.

## Required GitHub Secrets

Add these repository secrets:

- `GCP_WORKLOAD_IDENTITY_PROVIDER`
- `GCP_DEPLOY_SERVICE_ACCOUNT`

The workflow assumes:

- project ID: `kumbu-connect`
- region: `us-east1`
- service: `kumbu-connect-web`

## Suggested Deploy Service Account

Create a dedicated deploy service account such as:

```text
github-actions-deployer@kumbu-connect.iam.gserviceaccount.com
```

Grant it the minimum roles needed for your deploy path. A practical starting set is:

- `roles/run.admin`
- `roles/cloudbuild.builds.editor`
- `roles/artifactregistry.writer`
- `roles/iam.serviceAccountUser` on `kumbu-cloud-run@kumbu-connect.iam.gserviceaccount.com`

Depending on org policy and how you manage referenced secrets, you may also need narrowly scoped Secret Manager visibility for deployment validation.

## Workload Identity Federation

Create a GitHub OIDC workload identity provider in the `kumbu-connect` project and allow only this repository to impersonate the deploy service account.

Use the resulting provider resource name as `GCP_WORKLOAD_IDENTITY_PROVIDER`.

## Deploy Flow

The workflow authenticates with GitHub OIDC, installs `gcloud`, and runs:

```bash
SKIP_GCLOUD_CONFIG_CHECK=true ./deploy_cloud_run.sh
```

This keeps CI independent from any local `~/.config/gcloud` state.

## Remaining Manual Work

Cloud-side setup is still required once because current local Kumbu admin tokens are stale. After a Kumbu-owned admin re-auth, finish these steps in the `kumbu-connect` project:

1. create the deploy service account
2. bind the required IAM roles
3. create the GitHub workload identity provider
4. add the two GitHub repository secrets
5. test the workflow with `workflow_dispatch`