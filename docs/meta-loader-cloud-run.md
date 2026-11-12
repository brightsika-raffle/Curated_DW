# Migration Recommendation: Meta Ingestion Loader to Google Cloud Run

**Date:** 12 November 2026  
**Subject:** Migrating `meta_to_bq.py` from Windows Scheduled Task to Google Cloud Run Jobs  
**Status:** Architecture Recommendation  

---

## 1. Context & Motivation

The nightly Meta Graph API ingestion pipeline currently runs as a scheduled task (`\RH Meta to BigQuery`) in Windows Task Scheduler on a local machine. 

Running critical ingestion infrastructure on a local host introduces reliability vulnerabilities:
- Host sleep, restarts, or loss of network connectivity cause silent pipeline stops.
- Local Task Scheduler operational logging was disabled by default, preventing auditability when tasks are modified or disabled.
- The pipeline lacks centralised alerting, automated retry policies, and execution telemetry.

Migrating the loader to a serverless Google Cloud Run Job triggered by Cloud Scheduler eliminates host dependency, ensures reliable execution in `europe-west2`, and integrates with Cloud Logging and Monitoring.

---

## 2. Cost Analysis (Monthly)

The Meta loader executes once daily, fetching ad performance and audience segment breakdown data across a trailing 7–14 day window. Average execution duration is 30–60 seconds.

### Cost Breakdown:
- **Cloud Run Jobs:**
  - Invocations: 30 runs/month (well within Cloud Run free tier of 2,000,000 requests/month).
  - Compute (1 vCPU, 512 MiB RAM): ~30 runs x 45s = 1,350 vCPU-seconds / 675 GiB-seconds per month (free tier covers 360,000 vCPU-seconds and 180,000 GiB-seconds).
  - **Cloud Run Cost:** £0.00 / month.
- **Cloud Scheduler:**
  - 1 scheduled job triggering once daily at 21:00 UK time (first 3 jobs per Google Cloud account are free; otherwise £0.08 / month).
  - **Cloud Scheduler Cost:** £0.00 / month (or < £0.10 / month).
- **Artifact Registry:**
  - Storing a single ~150 MB container image (~£0.02 / month).
  - **Artifact Registry Cost:** < £0.05 / month.

**Total Estimated Cost:** **< £0.50 / month** (effectively zero under standard Google Cloud monthly free tier allowances).

---

## 3. Migration Steps

### Step 1: Containerise the Pipeline
Create a minimalist Dockerfile in the repository:
```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY meta_to_bq.py .
ENTRYPOINT ["python", "meta_to_bq.py"]
```

### Step 2: Push Image to Google Artifact Registry
```bash
gcloud artifacts repositories create pipeline-images \
    --repository-format=docker \
    --location=europe-west2 \
    --description="Docker repository for data ingestion pipelines"

gcloud builds submit --tag europe-west2-docker.pkg.dev/rh-ga4-491819/pipeline-images/meta-loader:latest .
```

### Step 3: Deploy as a Cloud Run Job
Create the Cloud Run Job using the existing pipeline service account:
```bash
gcloud run jobs create meta-loader-job \
    --image europe-west2-docker.pkg.dev/rh-ga4-491819/pipeline-images/meta-loader:latest \
    --region europe-west2 \
    --service-account rh-data-pipeline-rh-ga4-491819@rh-ga4-491819.iam.gserviceaccount.com \
    --set-env-vars PROJECT_ID=rh-ga4-491819,DATASET_ID=rh_curated_Bright_Test \
    --max-retries 2 \
    --task-timeout 10m
```
*Note: The existing service account already has Secret Manager Secret Accessor permission to fetch the `meta-access-token` secret.*

### Step 4: Schedule via Google Cloud Scheduler
Trigger the job nightly at 21:00 London time:
```bash
gcloud scheduler jobs create http meta-loader-nightly \
    --location europe-west2 \
    --schedule "0 21 * * *" \
    --time-zone "Europe/London" \
    --uri "https://europe-west2-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/rh-ga4-491819/jobs/meta-loader-job:run" \
    --http-method POST \
    --oauth-service-account-email rh-data-pipeline-rh-ga4-491819@rh-ga4-491819.iam.gserviceaccount.com
```

### Step 5: Decommission Local Task
Once verified against BigQuery for 3 consecutive days, disable and remove the local Windows Scheduled Task `\RH Meta to BigQuery`.
