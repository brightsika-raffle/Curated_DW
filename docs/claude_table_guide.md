# Claude BigQuery Table Guide: Raffle House Warehouse

## Standing Rules for Working with this Warehouse

1. **Sources stay where they are:** The source tables are the MongoDB/WooCommerce mirrors in `rh_warehouse` (`fact_transactions`, `fact_subscriptions`, `dim_customers`, `dim_raffles`, `dim_subscription_models`), plus `marketing_spend_by_channel_manual_upload`, the GA4 export `analytics_354198910`, `google_ads_transfer`, and the Meta API loads. Don't move or change them.
2. **Curated warehouse reads only sources:** The new warehouse (`rh_curated_Bright_Test`) reads only from those sources, or from other curated tables.
3. **Old warehouse retirement policy (NOTHING IS RETIRED YET):** The tables BUILT from the sources in `rh_warehouse` are what we'll retire eventually (e.g. `fact_transaction_attribution`, `customer_summary`, `customer_ltv_unified`, `subscriber_ltv_output`, `oneoff_customer_ltv_output`, curve tables, attribution rebuild output). Never read them in new work. If something only exists there, stop and tell the user, and we'll rebuild it from sources. NOTHING IS RETIRED YET. Don't drop, disable, pause, rename, or change any old table, view, or scheduled query (Jordan's or otherwise). They keep running as they are until every one of them has been rebuilt from sources and reconciled. That includes the paid media report the team needs now.
4. **Service account impersonation:** Run everything as `rh-data-pipeline-rh-ga4-491819@rh-ga4-491819.iam.gserviceaccount.com`. Use it per command (`--impersonate-service-account`), never as a global setting. No personal logins for pipelines, no keys, and don't grant any IAM roles.
5. **Cost controls:** Project `rh-ga4-491819`, location `europe-west2`. Dry-run first, and stop and ask before any query over 1 GB.

---

This document is the authoritative table guide to paste into Claude's instructions. Following these rules ensures Claude queries only pre-aggregated, cost-effective tables and avoids heavy scans on legacy or raw datasets.

---

## 1. Summary of Approved Tables for Reports & Claude

| Purpose | Approved Table / View | Grain | Date Partition / Filter Column | Example Query Cost |
|---|---|---|---|---|
| **Paid Media Reporting (Default)** | `rh-ga4-491819.rh_curated_Bright_Test.rpt_paid_media_page_daily` (or `vw_rpt_paid_media_page_daily`) | 1 row per `d` × `basis` ('F'/'L') × `channel` × `sub` | `d` (PARTITION) | ~0.14 MB |
| **Paid Media Pipeline Status** | `rh-ga4-491819.rh_curated_Bright_Test.rpt_paid_media_page_status` | 1 row status summary with max dates | `built_at` | ~0.001 MB |
| **Paid Media Warehouse Attribution** | `rh-ga4-491819.rh_curated_Bright_Test.rpt_paid_media_wh_daily` | 1 row per `click_date` × `basis` × `channel` × `subchannel` × `campaign` × `ct` | `click_date` (PARTITION) | ~0.10 MB |
| **Revenue & Orders** | `rh-ga4-491819.rh_curated_Bright_Test.fact_transactions` (or `vw_fact_transactions`) | 1 row per ACCEPTED order | `purchase_date` (PARTITION) | ~0.74 MB |
| **Channel Performance (Blended & ROAS)** | `rh-ga4-491819.rh_curated_Bright_Test.rpt_channel_perf_daily` | 1 row per `touch_date` × `channel` × `subchannel` | `touch_date` | ~0.02 MB |
| **Marketing Paid Spend** | `rh-ga4-491819.rh_curated_Bright_Test.fact_marketing_spend_daily` (or `vw_fact_marketing_spend_daily`) | 1 row per `spend_date` × `ad_platform` × `channel_group` × `campaign_id` | `spend_date` (PARTITION) | ~0.03 MB |
| **Customer Acquisition & Cohorts** | `rh-ga4-491819.rh_curated_Bright_Test.dim_customer` (or `vw_dim_customer`) | 1 row per customer (hashed email, no PII) | `acq_first_purchase_date` | ~19.76 MB |
| **Subscriptions** | `rh-ga4-491819.rh_curated_Bright_Test.fact_subscriptions` (or `vw_fact_subscriptions`) | 1 row per real subscription (baskets excluded) | `created_date` (PARTITION MONTH) | ~0.60 MB |
| **Subscription Tiers** | `rh-ga4-491819.rh_curated_Bright_Test.dim_subscription_model` (or `vw_dim_subscription_model`) | 1 row per model/tier (`tier_name`: Bronze, Silver, Gold, Platinum, Other) | None (~10 rows) | ~0.001 MB |
| **Customer LTV & Churn** | `rh-ga4-491819.rh_curated_Bright_Test.customer_ltv_horizons` | 1 row per customer with `current_tier_name` | `as_of_date` | ~13.38 MB |
| **Charity Performance** | `rh-ga4-491819.rh_curated_Bright_Test.rpt_charity_sales_daily` | 1 row per `date` × `selected_charity` | `date` | ~0.05 MB |
| **Business Health & Order Status** | `rh-ga4-491819.rh_curated_Bright_Test.fact_order_status_daily` (or `vw_fact_order_status_daily`) | 1 row per `order_date` × `payment_status` | `order_date` (PARTITION) | ~0.01 MB |

---

## 2. Approved Table Details & Verified Cheap Queries

### 2.1. Revenue & Orders
- **Table:** `rh-ga4-491819.rh_curated_Bright_Test.fact_transactions`
- **Grain:** One row per ACCEPTED order. Net revenue in pounds (`revenue_gbp`). Free ticket entries and baskets excluded.
- **Date Column:** `purchase_date` (Partition key).
- **Example Cheap Query (Dry Run: ~777 KB / 0.74 MB):**
```sql
SELECT
  purchase_date,
  COUNT(*) AS total_orders,
  ROUND(SUM(revenue_gbp), 2) AS total_revenue_gbp,
  SUM(tickets) AS total_tickets
FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_transactions`
WHERE purchase_date BETWEEN DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 30 DAY)
                        AND CURRENT_DATE('Europe/London')
GROUP BY 1
ORDER BY 1 DESC;
```

---

### 2.2. Daily Channel Performance (ROAS, CPA, CAC)
- **Table:** `rh-ga4-491819.rh_curated_Bright_Test.rpt_channel_perf_daily`
- **Grain:** Daily summary by channel and subchannel.
- **Date Column:** `touch_date`.
- **Example Cheap Query (Dry Run: ~16 KB / 0.02 MB):**
```sql
SELECT
  touch_date,
  channel,
  subchannel,
  wh_purchases,
  ROUND(wh_revenue, 2) AS wh_revenue,
  ROUND(spend_inc_vat, 2) AS spend_inc_vat,
  wh_roas_7d,
  wh_roas_12m_est
FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_channel_perf_daily`
WHERE touch_date >= DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 30 DAY)
ORDER BY touch_date DESC, wh_revenue DESC;
```

---

### 2.3. Marketing Ad Spend (Google Ads + Meta + Historical)
- **Table:** `rh-ga4-491819.rh_curated_Bright_Test.fact_marketing_spend_daily`
- **Grain:** One row per platform, channel group, campaign and device per date.
- **Date Column:** `spend_date` (Partition key).
- **Important Filter:** Filter `is_google_meta = TRUE` for core performance reporting.
- **Example Cheap Query (Dry Run: ~26 KB / 0.03 MB):**
```sql
SELECT
  spend_date,
  ad_platform,
  channel_group,
  ROUND(SUM(spend_inc_vat_gbp), 2) AS spend_inc_vat_gbp,
  SUM(clicks) AS clicks,
  SUM(impressions) AS impressions
FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_marketing_spend_daily`
WHERE spend_date >= DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 30 DAY)
GROUP BY 1, 2, 3
ORDER BY 1 DESC, 4 DESC;
```

---

### 2.4. Customer Cohorts & Acquisition Channels
- **Table:** `rh-ga4-491819.rh_curated_Bright_Test.dim_customer`
- **Grain:** One row per customer. Email is hashed (SHA-256). Contains fixed first-order acquisition attributes (`acq_*`).
- **Date Column:** `acq_first_purchase_date` (or `acq_cohort_month`).
- **Example Cheap Query (Dry Run: ~19.76 MB):**
```sql
SELECT
  acq_cohort_month,
  acq_channel_first_touch,
  COUNT(*) AS new_acquired_customers,
  ROUND(SUM(acq_first_order_gbp), 2) AS initial_revenue_gbp
FROM `rh-ga4-491819.rh_curated_Bright_Test.dim_customer`
WHERE acq_cohort_month >= '2026-01'
GROUP BY 1, 2
ORDER BY 1 DESC, 3 DESC;
```

---

### 2.5. Subscriptions (Active, Churned, Tiers)
- **Table:** `rh-ga4-491819.rh_curated_Bright_Test.fact_subscriptions`
- **Model Table:** `rh-ga4-491819.rh_curated_Bright_Test.dim_subscription_model`
- **Grain:** One row per subscription contract (baskets excluded).
- **Date Column:** `created_date` (Partitioned by month).
- **MANDATORY TIER NAMING RULE:** **Always say Bronze/Silver/Gold/Platinum, never Low/Mid/High.**
  - `Bronze`: £15 (and older £10)
  - `Silver`: £30 (and older £20 / £25)
  - `Gold`: £50 (and older £35)
  - `Platinum`: £75 (and older £55)
  - `Other`: any other price
  - Note: `tier_band` (Low/Mid/High/Very High) is retained solely as an internal survival curve grouping (Platinum folded into High for the curve). In all business summaries, reports, and Claude prompts, use `tier_name`!
- **Example Cheap Query (Dry Run: ~625 KB / 0.60 MB):**
```sql
SELECT
  DATE_TRUNC(created_date, MONTH) AS cohort_month,
  is_status_active,
  COUNT(*) AS subscription_count,
  ROUND(SUM(monthly_value_gbp), 2) AS total_monthly_committed_gbp
FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_subscriptions`
WHERE created_date >= '2026-01-01'
GROUP BY 1, 2
ORDER BY 1 DESC;
```

---

### 2.6. Customer LTV Horizons
- **Table:** `rh-ga4-491819.rh_curated_Bright_Test.customer_ltv_horizons`
- **Grain:** One row per customer with realized and projected forward LTV (3m, 6m, 12m, 36m).
- **Subscription Tier Column:** `current_tier_name` (the website tier name of the customer's active subscription: Bronze, Silver, Gold, Platinum; NULL if none).
- **Rule:** Always report **mean and median** together, as high-value outliers skew the mean by >200%.
- **Example Cheap Query (Dry Run: ~13.38 MB):**
```sql
SELECT
  ltv_segment,
  current_tier_name,
  COUNT(*) AS customer_count,
  ROUND(AVG(realised_value), 2) AS mean_realised_value,
  APPROX_QUANTILES(realised_value, 100)[OFFSET(50)] AS median_realised_value,
  ROUND(AVG(ltv_12m), 2) AS mean_12m_ltv,
  APPROX_QUANTILES(ltv_12m, 100)[OFFSET(50)] AS median_12m_ltv
FROM `rh-ga4-491819.rh_curated_Bright_Test.customer_ltv_horizons`
GROUP BY 1, 2
ORDER BY customer_count DESC;
```

---

### 2.7. Charity Performance
- **Table:** `rh-ga4-491819.rh_curated_Bright_Test.rpt_charity_sales_daily`
- **Grain:** Daily summary by charity and charity category.
- **Date Column:** `date`.
- **Example Cheap Query (Dry Run: ~56 KB / 0.05 MB):**
```sql
SELECT
  selected_charity,
  charity_type,
  SUM(purchases) AS total_orders,
  ROUND(SUM(revenue), 2) AS total_revenue_gbp
FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_charity_sales_daily`
WHERE date >= '2026-09-01'
GROUP BY 1, 2
ORDER BY total_revenue_gbp DESC
LIMIT 15;
```

---

### 2.8. Business Health & Gross vs Net Orders
- **Table:** `rh-ga4-491819.rh_curated_Bright_Test.fact_order_status_daily`
- **Grain:** Daily breakdown of orders by payment status (ACCEPTED, REFUNDED, REJECTED, PENDING_BASKET).
- **Date Column:** `order_date` (Partition key).
- **Example Cheap Query (Dry Run: ~6 KB / 0.01 MB):**
```sql
SELECT
  order_date,
  status_group,
  SUM(orders) AS orders_count,
  ROUND(SUM(value_gbp), 2) AS order_value_gbp
FROM `rh-ga4-491819.rh_curated_Bright_Test.fact_order_status_daily`
WHERE order_date >= DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 30 DAY)
GROUP BY 1, 2
ORDER BY 1 DESC, 3 DESC;
```

---

### 2.9. Paid Media Reporting (DEFAULT Source for Paid Media Questions)
- **Table:** `rh-ga4-491819.rh_curated_Bright_Test.rpt_paid_media_page_daily`
- **Status Table:** `rh-ga4-491819.rh_curated_Bright_Test.rpt_paid_media_page_status`
- **Grain:** One row per day (`d`) × attribution basis (`basis`: 'F' for first touch, 'L' for last touch) × channel (`Paid Social`, `Google`, `Bing`, `Affiliates`) × subchannel (`sub`: PR, RMG, RTG, Brand, Generic, Competitor, PMax, Demand Gen, All, Other).
- **Date Column:** `d` (Partition key).
- **Why Use This:** Pre-calculated nightly table combining inc-VAT spend, platform impressions/clicks/conversions, warehouse orders/revenue within 7 days, new acquired customers, new subscription customers, and 3-month LTV (`ltv3m`). Running reports against this table scans under 1 MB and costs virtually nothing.
- **Example Cheap Query (Dry Run: ~140 KB / 0.14 MB):**
```sql
SELECT
  d,
  channel,
  sub,
  spend,
  clicks,
  wh_orders,
  wh_rev,
  ROUND(SAFE_DIVIDE(wh_rev, spend), 2) AS roas,
  new_cust,
  new_subs,
  ltv3m
FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_paid_media_page_daily`
WHERE basis = 'L'
  AND d >= DATE_SUB(CURRENT_DATE('Europe/London'), INTERVAL 30 DAY)
ORDER BY d DESC, wh_rev DESC;
```

---

## 3. Tables to AVOID (Heavy, Outdated, or Raw)

Claude must **NEVER** scan or query the following tables unless explicitly requested for raw schema debugging:

| Dataset & Table | Size / Rows | Why Claude Must Avoid | What to Use Instead |
|---|---|---|---|
| `analytics_354198910.events_*` | 47.98 GB (555 shards, 41M rows) | Raw GA4 export. Unpartitioned sharding, deeply nested unnesting, extremely expensive and slow to query. | `rh_curated_Bright_Test.fact_ga4_purchases` or `rpt_channel_perf_daily` |
| `segmentstream.*` (679 tables) | 308.74 GB (582M rows) | **Abandoned pipeline.** All data ingestion stopped on 30 Aug 2026. Querying it wastes money on stale data. | `rh_curated_Bright_Test.fact_marketing_spend_daily` |
| `rh_warehouse.fact_transactions` | 0.62 GB (3.1M rows) | Legacy warehouse table. Contains raw pence, non-accepted orders, and un-curated data. | `rh_curated_Bright_Test.fact_transactions` |
| `rh_warehouse.fact_transaction_attribution` | 0.42 GB (3.1M rows) | Legacy attribution table being retired. | `rh_curated_Bright_Test.rpt_channel_perf_daily` |
| `rh_curated_Bright_Test.vw_channel_perf_daily_calc` | View (Live computation) | **Heavy view.** Dynamically live-queries raw GA4 event shards and multiple old warehouse tables on every query execution! | `rh_curated_Bright_Test.rpt_channel_perf_daily` (the pre-aggregated table) |
| `rh_warehouse.dim_customers` | 0.11 GB (445k rows) | Legacy table containing unhashed customer PII. Violates privacy rule. | `rh_curated_Bright_Test.dim_customer` |
| `rh_warehouse.marketing_spend_by_channel_manual_upload` | 0.0004 GB | Frozen manual spreadsheet. Contains old data up to September only. | `rh_curated_Bright_Test.fact_marketing_spend_daily` |
| `rh_curated_Bright_Test.stg_meta_ads_daily_ad` | 0.06 GB (161k rows) | Raw ad-level staging. Heavy for high-level questions. | `rh_curated_Bright_Test.fact_marketing_spend_daily` |
| `rh_derived.ga4_session_engagement` | 0.17 GB (1.8M rows) | Abandoned derivation table. Last modified August 2026. | `rh_curated_Bright_Test.rpt_channel_perf_daily` |

---

## 4. Hard Querying Rules for Claude
1. **Always filter on the partition column:** e.g., `purchase_date`, `spend_date`, `created_date`.
2. **Never scan raw GA4:** Always prefer `fact_ga4_purchases` or `rpt_channel_perf_daily`.
3. **Always report Mean AND Median:** Never present average order value or LTV without the median.
4. **No Customer PII:** Never output customer email, name, or customer IDs in report summaries.
5. **Dry Run First:** Check estimated bytes on any ad-hoc data query.
