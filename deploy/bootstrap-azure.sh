#!/usr/bin/env bash
set -euo pipefail

if [[ $# != 2 ]]; then
  echo "Usage: bash deploy/bootstrap-azure.sh AZURE_SUBSCRIPTION_ID GITHUB_OWNER/REPO" >&2
  exit 2
fi

cd "$(dirname "$0")/.."
subscription=$1
repo=$2
if [[ ! "$subscription" =~ ^[[:xdigit:]]{8}(-[[:xdigit:]]{4}){3}-[[:xdigit:]]{12}$ ]]; then
  echo "Supply the Azure subscription ID as a UUID." >&2
  exit 2
fi
group=sac26
region=eastus2
registry="sac26${subscription//-/}"
deploy_identity=sac26-deployer
pull_identity=sac26-pull

azure() {
  az "$@" --subscription "$subscription"
}

command -v az >/dev/null
command -v gh >/dev/null
gh repo view "$repo" --json nameWithOwner --jq .nameWithOwner >/dev/null
limit=$(azure rest --method get --url "https://management.azure.com/subscriptions/$subscription?api-version=2022-12-01" \
  --query subscriptionPolicies.spendingLimit -o tsv)
if [[ "$limit" != On ]]; then
  echo "Select an Azure trial subscription with its spending limit enabled." >&2
  exit 1
fi

temp_dir=$(mktemp -d)
trap 'rm -rf "$temp_dir"' EXIT
bash deploy/package-motion.sh "$temp_dir/motion-detector.tar.gz"

for provider in Microsoft.App Microsoft.ContainerRegistry Microsoft.ManagedIdentity Microsoft.Insights; do
  azure provider register --namespace "$provider" --wait
done
group_id=$(azure group create --name "$group" --location "$region" --query id -o tsv)
if ! azure acr show --resource-group "$group" --name "$registry" -o none 2>/dev/null; then
  azure acr create --resource-group "$group" --name "$registry" --sku Basic --location "$region" \
    --role-assignment-mode rbac --admin-enabled false -o none
fi
registry_id=$(azure acr show --resource-group "$group" --name "$registry" --query id -o tsv)
registry_host=$(azure acr show --resource-group "$group" --name "$registry" --query loginServer -o tsv)
azure acr config authentication-as-arm update --registry "$registry" --status enabled -o none
if ! azure containerapp env show --resource-group "$group" --name "$group" -o none 2>/dev/null; then
  azure containerapp env create --resource-group "$group" --name "$group" --location "$region" \
    --enable-workload-profiles false --logs-destination none -o none
fi

for identity in "$deploy_identity" "$pull_identity"; do
  if ! azure identity show --resource-group "$group" --name "$identity" -o none 2>/dev/null; then
    azure identity create --resource-group "$group" --name "$identity" --location "$region" -o none
  fi
done
deploy_principal=$(azure identity show --resource-group "$group" --name "$deploy_identity" --query principalId -o tsv)
deploy_client=$(azure identity show --resource-group "$group" --name "$deploy_identity" --query clientId -o tsv)
tenant=$(azure identity show --resource-group "$group" --name "$deploy_identity" --query tenantId -o tsv)
pull_principal=$(azure identity show --resource-group "$group" --name "$pull_identity" --query principalId -o tsv)
pull_id=$(azure identity show --resource-group "$group" --name "$pull_identity" --query id -o tsv)

grant() {
  azure role assignment create --assignee-object-id "$1" --assignee-principal-type ServicePrincipal \
    --role "$2" --scope "$3" -o none
}
grant "$deploy_principal" 'Container Apps Contributor' "$group_id"
grant "$deploy_principal" 'AcrPush' "$registry_id"
grant "$deploy_principal" 'Managed Identity Operator' "$pull_id"
grant "$pull_principal" 'AcrPull' "$registry_id"

federated=(--resource-group "$group" --identity-name "$deploy_identity" --name github-main)
subject="repo:$repo:ref:refs/heads/main"
issuer=https://token.actions.githubusercontent.com
if azure identity federated-credential show "${federated[@]}" -o none 2>/dev/null; then
  azure identity federated-credential update "${federated[@]}" --issuer "$issuer" \
    --subject "$subject" --audiences api://AzureADTokenExchange -o none
else
  azure identity federated-credential create "${federated[@]}" --issuer "$issuer" \
    --subject "$subject" --audiences api://AzureADTokenExchange -o none
fi

openssl base64 -A -in "$temp_dir/motion-detector.tar.gz" | gh secret set MOTION_DETECTOR_BUNDLE --repo "$repo"
gh variable set AZURE_CLIENT_ID --repo "$repo" --body "$deploy_client"
gh variable set AZURE_TENANT_ID --repo "$repo" --body "$tenant"
gh variable set AZURE_PULL_ID --repo "$repo" --body "$pull_id"
gh variable set AZURE_REGISTRY_NAME --repo "$repo" --body "$registry"
gh variable set AZURE_REGISTRY_HOST --repo "$repo" --body "$registry_host"
gh variable set AZURE_SUBSCRIPTION_ID --repo "$repo" --body "$subscription"
