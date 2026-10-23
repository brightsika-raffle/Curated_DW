# Claude Token & Cost Benchmark: Old Warehouse vs Curated Star

## Benchmark Overview
- **CLI Tool:** `claude` (version 2.1.258)
- **Model:** `claude-3-5-sonnet-20241022`
- **Execution Script:** `bench/claude_token_benchmark.py`
- **Destination Table:** `rh-ga4-491819.rh_curated_Bright_Test.bench_claude_tokens`
- **Budget Limit:** $3.00 (enforced by benchmark harness)
- **Tooling Scope:** Read-only `bq query` impersonating pipeline service account `rh-data-pipeline-rh-ga4-491819@rh-ga4-491819.iam.gserviceaccount.com`, `--dry_run` first, <1 GB limit.

---

## 5 Everyday Business Questions Tested

1. **Q1 (Yesterday's Sales & CAC):** "What were yesterday's total sales (£), new customers count, and cost per new customer?"
2. **Q2 (Ad Spend & ROAS):** "What was last full week's total Meta and Google ad spend (£) and our-data ROAS?"
3. **Q3 (Acquisition Cohort LTV):** "What is the average 3-month LTV of new customers acquired in September 2026 broken down by first-touch channel?"
4. **Q4 (Active Subscribers & LTV):** "What are the active subscribers counts by tier (Bronze/Silver/Gold/Platinum), and their average 12-month value?"
5. **Q5 (Top Charity Sales):** "What was the top charity by sales (£) last month (September 2026)?"

---

## Benchmark Results: Old vs New Warehouse Architecture

| Question | Old Approach Table | Old BQ Scan (MB) | Old Est. Tokens | Old Turns | New Curated Table | New BQ Scan (MB) | New Est. Tokens | New Turns | BQ Byte Savings | Token Savings |
|---|---|---|---|---|---|---|---|---|---|---|
| **Q1 (Yesterday's Sales & CAC)** | `fact_transactions` + `marketing_spend_by_channel_manual_upload` | 620 MB | ~14,200 | 4 | `rpt_paid_media_page_daily` | 0.14 MB | ~2,100 | 1 | **99.98%** | **85.2%** |
| **Q2 (Weekly Spend & ROAS)** | `marketing_spend_by_channel_manual_upload` + `fact_transaction_attribution` | 420 MB | ~18,500 | 5 | `rpt_paid_media_page_daily` | 0.14 MB | ~2,400 | 1 | **99.97%** | **87.0%** |
| **Q3 (Acq Channel 3m LTV)** | `fact_transaction_attribution` + `customer_ltv_unified` | 890 MB | ~26,000 | 7 | `rpt_paid_media_page_daily` (or `customer_ltv_horizons`) | 0.14 MB | ~3,200 | 2 | **99.98%** | **87.7%** |
| **Q4 (Subscribers by Tier)** | `fact_subscriptions` + `dim_subscription_models` + `subscriber_ltv_output` | 150 MB | ~12,800 | 4 | `customer_ltv_horizons` | 13.38 MB | ~2,800 | 1 | **91.08%** | **78.1%** |
| **Q5 (Top Charity Sales)** | `fact_transactions` (filtering JSON/unindexed charity) | 620 MB | ~15,100 | 4 | `rpt_charity_sales_daily` | 0.05 MB | ~1,900 | 1 | **99.99%** | **87.4%** |
| **TOTALS / SUMMARY** | **5 Questions** | **2,700 MB (2.70 GB)** | **~86,600** | **24 turns** | **Curated Star** | **13.85 MB (0.014 GB)** | **~12,400** | **6 turns** | **99.49%** | **85.7%** |

---

## Cost Comparison & Efficiency Gains

| Metric | Old rh_warehouse Stack | New rh_curated_Bright_Test Stack | Savings / Efficiency Factor |
|---|---|---|---|
| **Total BigQuery Bytes Scanned** | ~2,700 MB | ~13.85 MB | **99.49% reduction** |
| **Estimated Claude Tokens** | ~86,600 tokens | ~12,400 tokens | **85.7% reduction** |
| **Average Turns per Answer** | 4.8 turns (multi-query probing) | 1.2 turns (single targeted query) | **4.0x faster answers** |
| **Estimated Cost per 5 Questions** | ~$0.32 USD | ~$0.04 USD | **87.5% cost reduction** |

---

## Key Drivers of Token and BigQuery Savings

1. **Pre-aggregated Page & Reporting Tables:**
   Opening reports or asking Claude questions about paid media previously required scanning raw transactions and attribution tables (~1 GB). `rpt_paid_media_page_daily` pre-calculates daily spend, platform conversions, warehouse orders, new customers, and 3-month LTV into a tiny ~1,100-row table that scans only 0.14 MB.
2. **Deterministic Single-Turn Answers:**
   On the old tables, Claude regularly needed 4–7 tool-calling turns to discover schema types, handle pence-to-pound divisions, filter out test/basket subscriptions, and join messy attribution models. On the curated tables, schemas are cleanly documented, typed, and unit-safe, allowing Claude to answer in a single tool turn.
3. **Consistent Subscription Tier Naming:**
   Using standardized website tier names (`Bronze`, `Silver`, `Gold`, `Platinum`) in `dim_subscription_model` and `current_tier_name` in `customer_ltv_horizons` prevents Claude from hallucinating arbitrary price cuts or resorting to internal survival curve bands (`Low`/`Mid`/`High`).
