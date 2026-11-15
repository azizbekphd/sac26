# Deploy Planet X and SPHEREx

The container serves the Vite build and FastAPI from the same origin. Cloud Run runs one Uvicorn worker on one instance with CPU available between requests. Playback and spectrum jobs live in memory, so a restart or deployment clears active jobs. This deployment uses South Carolina (`us-east1`), near the public SPHEREx archive in AWS `us-east-1`.

## First deployment

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

## Connect `phds-sac26.earth`

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
