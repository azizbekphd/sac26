#!/usr/bin/env bash
set -euo pipefail

if [[ $# != 2 ]]; then
  echo "Usage: bash deploy/bootstrap-cloud-run.sh GOOGLE_CLOUD_PROJECT GITHUB_OWNER/REPO" >&2
  exit 2
fi

cd "$(dirname "$0")/.."
project=$1
repo=$2
region=us-east1
service=sac26
pool=sac26-github
deployer="sac26-deployer@$project.iam.gserviceaccount.com"
runtime="sac26-runtime@$project.iam.gserviceaccount.com"
bucket="gs://$project-sac26-motion"
python=${MOTION_PYTHON:-.venv/bin/python}

command -v gcloud >/dev/null
command -v gh >/dev/null
ids=$(gh api "repos/$repo" --jq '[.id, .owner.id] | @tsv')
read -r repo_id owner_id <<< "$ids"
[[ "$repo_id" =~ ^[0-9]+$ && "$owner_id" =~ ^[0-9]+$ ]]
project_number=$(gcloud projects describe "$project" --format='value(projectNumber)')
[[ "$project_number" =~ ^[0-9]+$ ]]

bundle=$("$python" -c 'from server.motion_adapter import ROOT, verify_frozen; verify_frozen(); print(ROOT)')
temp_dir=$(mktemp -d)
trap 'rm -rf "$temp_dir"' EXIT
tar -C "$bundle" -czf "$temp_dir/motion-detector.tar.gz" \
  requirements.txt frozen-source-manifest.json source-validation-manifest.json \
  frozen_revised/{__init__,archive,baseline,calibration,coverage,proposals}.py

export CLOUDSDK_CORE_PROJECT="$project"
gcloud services enable run.googleapis.com artifactregistry.googleapis.com storage.googleapis.com \
  iam.googleapis.com iamcredentials.googleapis.com sts.googleapis.com cloudresourcemanager.googleapis.com

if ! gcloud artifacts repositories describe "$service" --location "$region" >/dev/null 2>&1; then
  gcloud artifacts repositories create "$service" --location "$region" --repository-format docker
fi
if ! gcloud storage buckets describe "$bucket" >/dev/null 2>&1; then
  gcloud storage buckets create "$bucket" --location "$region" --uniform-bucket-level-access \
    --public-access-prevention
fi
gcloud storage cp "$temp_dir/motion-detector.tar.gz" "$bucket/motion-detector.tar.gz"

for account in sac26-deployer sac26-runtime; do
  if ! gcloud iam service-accounts describe "$account@$project.iam.gserviceaccount.com" >/dev/null 2>&1; then
    gcloud iam service-accounts create "$account"
  fi
done
gcloud projects add-iam-policy-binding "$project" --member "serviceAccount:$deployer" \
  --role roles/run.developer --condition=None >/dev/null
gcloud artifacts repositories add-iam-policy-binding "$service" --location "$region" \
  --member "serviceAccount:$deployer" --role roles/artifactregistry.writer >/dev/null
gcloud storage buckets add-iam-policy-binding "$bucket" \
  --member "serviceAccount:$deployer" --role roles/storage.objectViewer >/dev/null
gcloud iam service-accounts add-iam-policy-binding "$runtime" \
  --member "serviceAccount:$deployer" --role roles/iam.serviceAccountUser >/dev/null

if ! gcloud iam workload-identity-pools describe "$pool" --location global >/dev/null 2>&1; then
  gcloud iam workload-identity-pools create "$pool" --location global
fi
provider_flags=(
  --workload-identity-pool "$pool" --location global
  --attribute-mapping 'google.subject=assertion.sub,attribute.repository_id=assertion.repository_id'
  --attribute-condition "assertion.repository_id=='$repo_id' && assertion.repository_owner_id=='$owner_id' && assertion.ref=='refs/heads/main' && assertion.workflow_ref.endsWith('/.github/workflows/deploy.yml@refs/heads/main')"
)
if gcloud iam workload-identity-pools providers describe github --workload-identity-pool "$pool" --location global >/dev/null 2>&1; then
  gcloud iam workload-identity-pools providers update-oidc github "${provider_flags[@]}"
else
  gcloud iam workload-identity-pools providers create-oidc github "${provider_flags[@]}" \
    --issuer-uri https://token.actions.githubusercontent.com
fi
pool_resource="projects/$project_number/locations/global/workloadIdentityPools/$pool"
gcloud iam service-accounts add-iam-policy-binding "$deployer" --role roles/iam.workloadIdentityUser \
  --member "principalSet://iam.googleapis.com/$pool_resource/attribute.repository_id/$repo_id" >/dev/null

gh variable set GCP_DEPLOYER_SERVICE_ACCOUNT --repo "$repo" --body "$deployer"
gh variable set GCP_WORKLOAD_IDENTITY_PROVIDER --repo "$repo" --body "$pool_resource/providers/github"
gh variable set GCP_PROJECT_ID --repo "$repo" --body "$project"
