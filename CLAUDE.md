# Standing Rules for Raffle House Data Warehouse

0. **Numbers must come from code:**
   Every number in a report back, whether ours, a reference value or a difference, must be printed by a committed script under `scripts/` that reads the sources directly (BigQuery, `data2.json` and so on). Paste the script's raw output. Never type, round or estimate a figure by hand. If a script fails, say it failed. Don't fill in values.

1. **Sources stay where they are:**
   The source tables are the MongoDB/WooCommerce mirrors in `rh_warehouse` (`fact_transactions`, `fact_subscriptions`, `dim_customers`, `dim_raffles`, `dim_subscription_models`), plus `marketing_spend_by_channel_manual_upload`, the GA4 export `analytics_354198910`, `google_ads_transfer`, and the Meta API loads. Don't move or change them.
2. **Curated warehouse reads only sources:**
   The new warehouse (`rh_curated_Bright_Test`) reads only from those sources, or from other curated tables.
3. **Old warehouse retirement policy (NOTHING IS RETIRED YET):**
   The tables BUILT from the sources in `rh_warehouse` are what we'll retire eventually. For example `fact_transaction_attribution`, `customer_summary`, `customer_ltv_unified`, `subscriber_ltv_output`, `oneoff_customer_ltv_output`, the curve tables, and the attribution rebuild output. Never read them in new work. If something only exists there, stop and tell the user, and we'll rebuild it from sources. NOTHING IS RETIRED YET. Don't drop, disable, pause, rename or change any old table, view or scheduled query (Jordan's or otherwise). They keep running as they are until every one of them has been rebuilt from sources and reconciled. That includes the paid media report the team needs now.
4. **Service account impersonation:**
   Run everything as `rh-data-pipeline-rh-ga4-491819@rh-ga4-491819.iam.gserviceaccount.com`. Use it per command (`--impersonate-service-account`), never as a global setting. No personal logins for pipelines, no keys, and don't grant any IAM roles.
5. **Cost controls:**
   Project `rh-ga4-491819`, location `europe-west2`. Dry-run first, and stop and ask before any query over 1 GB.
