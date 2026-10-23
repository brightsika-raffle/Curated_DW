# Raffle House Organic Social Reporting Architecture

## 1. Overview & Goal
This suite provides an automated, low-cost weekly organic social report for Raffle House (Instagram, Facebook, and TikTok). It captures site-side traffic and purchases from organic social referrals and is architected to ingest native Meta Graph API account and post-level engagement once permissions are enabled.

All heavy computation is performed in BigQuery, and reports read lightweight pre-aggregated tables to avoid unnecessary queries and token consumption.

---

## 2. Table Specifications & Data Dictionary

### A. `rh_curated_Bright_Test.vw_organic_social_sessions_daily` (View)
Daily site-side traffic and orders from organic social referrals.
- **Grain:** `date` × `platform` × `traffic_category`
- **Columns:**
  - `date` (DATE): Session / purchase date.
  - `platform` (STRING): 'Instagram', 'Facebook', 'TikTok'.
  - `traffic_category` (STRING):
    - `clearly_organic`: GA4 untagged social referrals (`facebook.com`, `instagram.com`, `tiktok.com`, `l.facebook.com`, etc.) with no paid UTM medium or paid campaign tag.
    - `unclear`: Social visits carrying ambiguous non-standard campaigns (e.g. `House_*` or untracked `fbclid` with no matching Meta ad ID).
  - `sessions` (INTEGER): Total sessions from `ga4_source_daily`.
  - `visitors` (INTEGER): Distinct visitors.
  - `purchases` (INTEGER): Net non-renewal, non-free purchases (`ACCEPTED` status).
  - `revenue` (NUMERIC): Net purchase revenue (£).
  - `new_customers` (INTEGER): First-ever purchaser count among them.

### B. `rh_curated_Bright_Test.rpt_organic_social_weekly` (Table)
Weekly aggregation partitioned on `week_commencing` (Monday) and clustered on `platform`.
- **Grain:** `week_commencing` × `platform` ('Instagram', 'Facebook', 'TikTok', 'Total')
- **Columns:**
  - `week_commencing` (DATE): Monday of the week.
  - `platform` (STRING): Platform name or 'Total'.
  - `followers_start` (INTEGER): Followers at start of week (NULL until Meta connected).
  - `followers_end` (INTEGER): Followers at end of week (NULL until Meta connected).
  - `follower_change` (INTEGER): Net follower change (NULL until Meta connected).
  - `reach` (INTEGER): Unique accounts reached (NULL until Meta connected).
  - `total_engagements` (INTEGER): Likes, comments, saves, shares (NULL until Meta connected).
  - `engagement_rate` (NUMERIC): Engagements ÷ reach (NULL until Meta connected).
  - `posts_published` (INTEGER): Number of posts (NULL until Meta connected).
  - `posts_by_type` (STRING): Breakdown by post/reel/carousel/story (NULL until Meta connected).
  - `mean_reach_per_post` (NUMERIC): Average reach per post (NULL until Meta connected).
  - `median_reach_per_post` (NUMERIC): Median reach per post (NULL until Meta connected).
  - `mean_engagement_rate_per_post` (NUMERIC): Average post engagement rate (NULL until Meta connected).
  - `median_engagement_rate_per_post` (NUMERIC): Median post engagement rate (NULL until Meta connected).
  - `has_outlier_post` (BOOLEAN): Flag if any post > 3× median or > 99th percentile (NULL until Meta connected).
  - `site_sessions` (INTEGER): Clearly organic site visits.
  - `site_purchases` (INTEGER): Clearly organic purchases.
  - `site_revenue` (NUMERIC): Clearly organic revenue (£).
  - `site_new_customers` (INTEGER): Clearly organic new customers.
  - `site_sessions_unclear` (INTEGER): Ambiguous/unclear social sessions.
  - `site_purchases_unclear` (INTEGER): Ambiguous/unclear social purchases.
  - `site_revenue_unclear` (NUMERIC): Ambiguous/unclear social revenue (£).
  - `site_new_customers_unclear` (INTEGER): Ambiguous/unclear social new customers.

### C. `rh_curated_Bright_Test.rpt_organic_social_top_posts` (Table)
Top 10 posts per week ranked by engagement rate (populated once Meta feed is active).
- **Columns:** `week_commencing`, `platform`, `post_rank`, `post_id`, `permalink`, `post_type`, `reach`, `engagement`, `engagement_rate`.

### D. `rh_curated_Bright_Test.rpt_organic_social_status` (Table, 1 row)
Health and sync status for organic social monitoring.
- **Columns:**
  - `built_at` (TIMESTAMP): Build execution timestamp.
  - `meta_connected` (BOOLEAN): Current feed state (`FALSE`).
  - `ig_data_to` (DATE): Latest Instagram insights date (`NULL`).
  - `fb_data_to` (DATE): Latest Facebook Page insights date (`NULL`).
  - `site_data_to` (DATE): Latest site data available (`2026-10-22`).

---

## 3. Proposal: "Organic Social" Channel Grouping

### Background
Currently, Raffle House warehouse attribution maps all Meta-associated traffic (`utm_source` in `meta`, `facebook`, `fb`, `instagram`, `ig` or landing URL with `fbclid`) into **Paid Social**. When a visit arrives with no UTM tags from `instagram.com` or `facebook.com`, it defaults to **Direct / untagged** or **Unattributed**.

### Proposed Change
Add an explicit channel rule for `Organic Social` with two distinct tiers:
1. **Clearly Organic**: Untagged referrals from `facebook.com`, `instagram.com`, `tiktok.com` (and mobile wrappers `m.facebook.com`, `l.instagram.com`).
2. **Unclear / Ambiguous**: Non-ad campaigns (e.g. `House_*`, organic PR posts) or untracked `fbclid` with no corresponding ad ID in `stg_meta_ads_daily_ad`.

### Impact (1 Sep – 22 Oct 2026)
- **Clearly Organic Reclassified:**
  - **159 purchases** and **£3,346.00 revenue** move out of Direct / Untagged into Organic Social.
- **Unclear Social Isolated:**
  - **198 purchases** and **£4,282.00 revenue** separated from Paid Social into a dedicated "Possibly Organic (Unclear)" monitoring bucket rather than skewing paid ROAS.

---

## 4. Meta System User Permissions Guide (for Maddy)

The existing system user token in Secret Manager (`meta-access-token`) is actively feeding paid media via the `ads_read` permission, but lacks organic Page and Instagram permissions.

To connect the organic feed:
1. Log in to [Meta Business Suite / Business Settings](https://business.facebook.com).
2. Go to **Users** > **System Users** > select **RH Data Pipeline** (User ID: `122108617803487535`).
3. Under **Assigned Assets**:
   - Click **Add Assets** > **Pages** > select **Raffle House Facebook Page** > toggle on **Read Content** and **View Page Performance (insights)**.
   - Click **Add Assets** > **Instagram Accounts** > select **Raffle House Instagram Account** > toggle on **Insights** and **Read**.
4. Under **Generate Token / App Permissions**:
   Ensure the following 4 permissions are granted alongside `ads_read`:
   - `pages_read_engagement`
   - `read_insights`
   - `instagram_basic`
   - `instagram_manage_insights`
5. Save and update Secret Manager secret `meta-access-token`. No app or secret re-creation is required.
