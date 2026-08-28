# CI Deployment For Cloud Run

This repo now includes a GitHub Actions workflow at [../.github/workflows/deploy-cloud-run.yml](../.github/workflows/deploy-cloud-run.yml) so Cloud Run deploys can move off a mixed-use laptop.

## Recommended Model

Use GitHub Actions with Workload Identity Federation and a dedicated Kumbu deploy service account.

That keeps deploy authority in the `kumbuconnect1` project and avoids long-lived JSON keys on developer laptops.

## Required GitHub Secrets

Add these repository secrets:

- `GCP_WORKLOAD_IDENTITY_PROVIDER`
- `GCP_DEPLOY_SERVICE_ACCOUNT`

For this repo, the values are:

```text
GCP_WORKLOAD_IDENTITY_PROVIDER=projects/62809838048/locations/global/workloadIdentityPools/github-actions/providers/kumbu-connect-github
GCP_DEPLOY_SERVICE_ACCOUNT=github-actions-deployer@kumbuconnect1.iam.gserviceaccount.com
```

The workflow assumes:

- project ID: `kumbuconnect1`
- region: `us-east1`
- service: `kumbu-connect-web`

## Suggested Deploy Service Account

Create a dedicated deploy service account such as:

```text
github-actions-deployer@kumbuconnect1.iam.gserviceaccount.com
```

Grant it the minimum roles needed for your deploy path. A practical starting set is:

- `roles/run.admin`
- `roles/cloudbuild.builds.editor`
- `roles/artifactregistry.writer`
- `roles/iam.serviceAccountUser` on `kumbu-cloud-run@kumbuconnect1.iam.gserviceaccount.com`

Depending on org policy and how you manage referenced secrets, you may also need narrowly scoped Secret Manager visibility for deployment validation.

## Workload Identity Federation

Create a GitHub OIDC workload identity provider in the `kumbuconnect1` project and allow only this repository to impersonate the deploy service account.

Use the resulting provider resource name as `GCP_WORKLOAD_IDENTITY_PROVIDER`.

After `gh auth login`, you can publish the two repository secrets with:

```bash
./set_github_actions_repo_secrets.sh
```

## Deploy Flow

The workflow authenticates with GitHub OIDC, installs `gcloud`, and runs:

```bash
SKIP_GCLOUD_CONFIG_CHECK=true ./deploy_cloud_run.sh
```

This keeps CI independent from any local `~/.config/gcloud` state.

## Remaining Manual Work

Cloud-side setup is already in place for `kumbuconnect1`; the remaining manual steps are:

1. authenticate GitHub CLI with `gh auth login`
2. run `./set_github_actions_repo_secrets.sh`
3. test the workflow with `workflow_dispatch`