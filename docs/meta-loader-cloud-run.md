# Meta Ingestion Loader Migration to Google Cloud Run

**Date:** 12 November 2026  
**Subject:** Migration of `meta_to_bq.py` from Windows Scheduled Task to Google Cloud Run Jobs  
**Status:** Completed & Active in Production  

---

## 1. Executive Summary

The nightly Meta Graph API ingestion pipeline has been migrated from local execution via Windows Task Scheduler (`\RH Meta to BigQuery`) to a fully managed Google Cloud Run Job (`meta-ads-loader`) in `europe-west2`, triggered nightly at 21:00 London time by Google Cloud Scheduler (`meta-ads-loader-nightly`).

The Windows Scheduled Task has been disabled and placed in a 7-day retention period for instant rollback if required.

---

## 2. Architecture & Infrastructure Configuration

| Component | Identifier / Configuration |
|---|---|
| **Google Cloud Project** | `rh-ga4-491819` |
| **Region** | `europe-west2` (London) |
| **Artifact Registry** | `europe-west2-docker.pkg.dev/rh-ga4-491819/meta-loader-repo/meta-ads-loader` |
| **Image Digest** | `sha256:ba3a6e9b667a489929759bc61e8d4882def0775a256312f4b7962998c7a532a3` |
| **Cloud Run Job** | `meta-ads-loader` |
| **Compute Profile** | 1 vCPU, 512 MiB RAM, 1 Task |
| **Job Timeout** | 30 minutes |
| **Max Retries** | 2 |
| **Service Account** | `rh-data-pipeline-rh-ga4-491819@rh-ga4-491819.iam.gserviceaccount.com` |
| **Cloud Scheduler Job** | `meta-ads-loader-nightly` |
| **Trigger Schedule** | `0 21 * * *` (21:00 Europe/London daily) |
| **Trigger Auth** | OAuth token via pipeline service account |

---

## 3. Data Flow & Tables Loaded

The container executes `python meta_to_bq.py` in default rolling window mode (7 days back to yesterday), writing to the following curated staging tables in `rh-ga4-491819.rh_curated_Bright_Test`:
1. `stg_meta_ads_daily` — Daily campaign spend, impressions, clicks, reach, conversions.
2. `stg_meta_segment_daily` — Audience segment performance (`prospecting`, `engaged`, `existing`, `unknown`).
3. `stg_meta_campaign_reach` — Rolling 7-day and 30-day deduplicated campaign reach.
4. `stg_meta_breakdown_daily` — Demographic and regional breakdowns.
5. `gs://rh-ga4-491819-raw/meta/` — Raw API payload archival.

---

## 4. Cost Analysis

Execution profile:
- 1 run daily, 30–60 seconds duration at 512 MiB memory and 1 vCPU.
- Cloud Run Jobs monthly compute: ~30 runs x 45s = 1,350 vCPU-seconds and 675 GiB-seconds (fully covered by Google Cloud monthly free tier of 360,000 vCPU-s and 180,000 GiB-s).
- Cloud Scheduler: 1 job (£0.00 within free tier of 3 free jobs/month).
- Artifact Registry: Single container image ~120 MB (< £0.05 / month).

**Total Monthly Cost:** **< £0.50 / month** (effectively £0.00).

---

## 5. Verification & Switch-over (CR1.4)

On 12 November 2026, the Cloud Run Job was executed manually (`meta-ads-loader-zgrhz`). The output was compared side-by-side with direct laptop execution of `meta_to_bq.py` for `2026-11-11`:
- `stg_meta_segment_daily`: 20 rows, £1,670.79 spend (matched to the penny).
- `stg_meta_ads_daily`: 3 rows, £1,670.76 spend (matched to within £0.03 live API adjustment window).

The local scheduled task `\RH Meta to BigQuery` was subsequently disabled:
```powershell
Get-ScheduledTask -TaskName "RH Meta to BigQuery"
# TaskPath: \  TaskName: RH Meta to BigQuery  State: Disabled
```
The local task remains disabled for 7 days before final deletion.
