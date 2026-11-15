# Deploy Planet X and SPHEREx

The container serves the Vite build and FastAPI from the same origin. Each deployment runs one Uvicorn worker on one replica with CPU available between requests. Playback and spectrum jobs live in memory, so a restart or deployment clears active jobs.

## Render Free

Create a [Free web service](https://render.com/docs/free) in a Hobby workspace with no payment method. Connect only the `sac26` repository, and choose its `main` branch and the Docker runtime. Set the health check path to `/`. Choose **After CI Checks Pass** for automatic deployment.

Add `motion-detector.b64` as a [Render secret file](https://render.com/docs/docker-secrets) using the base64 output from these commands. Keep the output in the secret file editor.

```sh
bundle_dir=$(mktemp -d)
bash deploy/package-motion.sh "$bundle_dir/motion-detector.tar.gz"
openssl base64 -A -in "$bundle_dir/motion-detector.tar.gz"
rm -r "$bundle_dir"
```

If the detector bundle changes, use **Clear build cache & deploy** so Docker extracts the new secret. Once the generated Render URL serves the viewer and API, add `phds-sac26.earth` under **Custom Domains** and apply the DNS records Render provides in Porkbun. Render adds the `www` redirect for an apex domain. Verify the managed certificate and both `/` and `/embed` over HTTPS. The Free instance has 512 MB of RAM and spins down after 15 minutes without traffic.

### SPHEREx exposure snapshot

Run **Refresh SPHEREx exposure index** from GitHub Actions on `main` for an on-demand snapshot. The workflow checks daily and exports a new snapshot about once a week. It retries daily when the initial export or a scheduled refresh fails. It exports DR1 and QR3 Level 2 footprints from IRSA, including the deep surveys, and builds a SQLite R-tree. It publishes the compressed database as a versioned asset on this repository's GitHub Releases and updates [the lock file](spherex-index.lock) only after the asset uploads. Keep Render's auto-deploy mode set to **After CI Checks Pass**. The refresh dispatches the verification workflow for the updated lock, then checks the live snapshot timestamp and counts and requests a new S3 exposure preview on Render.

Docker verifies the asset against the lock file and bundles the uncompressed SQLite file into the image. Render reads it from disk on each lookup, including after spin-down. The previous image and release asset remain available when an export, validation, or build fails. With an empty lock, the viewer uses live IRSA metadata. Check `GET /api/archive-index` for the bundled snapshot timestamp and row counts.

For a local refresh and lookup:

```sh
PYTHONPATH=. python deploy/build-spherex-index.py .cache/spherex.sqlite3
SPHEREX_INDEX_PATH="$PWD/.cache/spherex.sqlite3" python -m uvicorn server.app:app
```

## Azure free trial

The Azure free account provides up to $200 in credit for 30 days to eligible users. Keep the subscription spending limit enabled. Azure stops the service when the credit is exhausted or expires. The bootstrap checks that the spending limit is enabled before it creates the resource group, the Basic container registry and the Consumption environment. It limits GitHub's deployment identity to the `main` branch of the supplied repository.

1. Authenticate the [Azure CLI](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli) and [GitHub CLI](https://cli.github.com/). Select the Azure trial subscription and run the bootstrap from the repository root. It packages the validated local detector and stores the compressed bundle in a GitHub Actions secret. Use `MOTION_PYTHON` to select a different Python environment with the validated detector installed.

   ```sh
   bash deploy/bootstrap-azure.sh SUBSCRIPTION_ID OWNER/REPO
   ```

2. Run **Verify and deploy** on `main`. The `deploy-azure` job starts one Container Apps replica in East US 2 and checks the generated HTTPS endpoint. The instance has 1 vCPU and 2 GiB of memory. The detector timeout is 230 seconds so it can return an error before Azure's 240-second ingress timeout.
3. In Porkbun DNS, replace the apex parking records with an `A` record for the environment's static IP. Add the `asuid` TXT record for the app's verification ID. Use the following commands to get both values.

   ```sh
   az containerapp env show --name sac26 --resource-group sac26 --query properties.staticIp -o tsv
   az containerapp show --name sac26 --resource-group sac26 --query properties.customDomainVerificationId -o tsv
   ```

4. In the app's **Custom domains** page, add `phds-sac26.earth` with the [free managed certificate](https://learn.microsoft.com/en-us/azure/container-apps/custom-domains-managed-certificates). Once the domain is marked **Secured**, check `https://phds-sac26.earth/` and `https://phds-sac26.earth/embed`.

## Google Cloud Run first deployment

This deployment uses South Carolina (`us-east1`), near the public SPHEREx archive in AWS `us-east-1`. GitHub uses Azure when Azure configuration is present.

1. Select a dedicated Google Cloud project with active trial credits or authorized billing. Authenticate the [Google Cloud CLI](https://cloud.google.com/sdk/docs/install) and [GitHub CLI](https://cli.github.com/) with permission to configure the project and repository.
2. From the repository root, run the bootstrap with the project ID and GitHub repository. It verifies the local motion detector, uploads only the frozen sources and requirements to a private Cloud Storage bucket, and configures Artifact Registry and GitHub OIDC. Use `MOTION_PYTHON` to select a different Python environment with the validated detector installed.

   ```sh
   bash deploy/bootstrap-cloud-run.sh PROJECT_ID OWNER/REPO
   ```

3. Push to `main` or run **Verify and deploy** from the GitHub Actions tab. A successful run builds, smoke-tests and deploys the container. Pull requests run the Python and browser tests.
4. After the first deployment, make the service public once with the project admin account and check its generated URL.

   ```sh
   gcloud run services update sac26 --project PROJECT_ID --region us-east1 --no-invoker-iam-check
   gcloud run services describe sac26 --project PROJECT_ID --region us-east1 --format='value(status.url)'
   ```

## Connect `phds-sac26.earth` to Cloud Run

1. [Verify ownership in Google Search Console](https://search.google.com/search-console/welcome) using the TXT record it provides. Manage DNS in [Porkbun](https://kb.porkbun.com/article/231-how-to-add-dns-records-on-porkbun).
2. Create a [Cloud Run domain mapping](https://cloud.google.com/run/docs/mapping-custom-domains#run) for the root domain using the verified Google account. This feature is in preview.

   ```sh
   gcloud beta run domain-mappings create --project PROJECT_ID --region us-east1 \
     --service sac26 --domain phds-sac26.earth
   gcloud beta run domain-mappings describe --project PROJECT_ID --region us-east1 \
     --domain phds-sac26.earth --format='yaml(status.resourceRecords)'
   ```

3. Replace the apex parking records in Porkbun with the records Cloud Run returns. Once Cloud Run issues the managed certificate, check `https://phds-sac26.earth/` and `https://phds-sac26.earth/embed`.

The 1 vCPU and 2 GiB instance costs about $57 per 30 days at [list prices](https://cloud.google.com/run/pricing) before free-tier discounts, registry storage and network transfer. To stop instance charges when the demo is over, use `gcloud run services update sac26 --project PROJECT_ID --region us-east1 --scaling=0`. A subsequent successful deployment starts it again.
