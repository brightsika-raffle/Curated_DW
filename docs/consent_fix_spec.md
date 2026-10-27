# Technical Specification: GA4 Consent Mode & Landing Attribution Fix

**Author:** Bright Sika (`bright@raffle-house.com`)  
**Target Audience:** Web Engineering & Google Tag Manager (GTM) Implementation Team  
**Status:** Approved Specification (Implementation Only — Do Not Change Marketing Campaigns)  
**Date:** 27 October 2026  
**Document Path:** `docs/consent_fix_spec.md`  

---

## 1. Executive Summary & Root Cause

Following the deployment of the "Cookies Yes" consent banner on 19 October and a consent default adjustment on 24 October:
1. **Source Loss in GA4:** Unassigned / untagged sessions in GA4 surged from 1–3% to ~80% (19 Oct) and ~86% (24 Oct).
2. **Session Count Inflation:** GA4 session starts rose by 103% while page views grew 34% and orders grew 24%. Because declined users have `privacy_info.analytics_storage = 'No'`, GA4 cannot persist client-side cookies (`_ga`, `_ga_<container-id>`) across navigations. Every single subsequent page interaction or reload initiates a separate unstitched `session_start` ping.
3. **Attribution Loss on Consent Acceptance:** When a visitor lands with query parameters (e.g. `utm_source`, `utm_medium`, `gclid`, `fbclid`), the initial page load occurs prior to consent interaction. If the user clicks "Accept" after navigating to another page (or after initial cookieless pings), the consent update (`gtag('consent', 'update', ...)`) fires without reapplying the initial landing URL parameters, causing GA4 to attribute the stitched session to Direct / Unassigned.
4. **Ad Click ID Stripping on Single Page Application (SPA):** Internal navigations strip query parameters from the browser address bar, breaking cross-page attribution when cookies are initially withheld.

This specification provides the exact, copy-pasteable GTM configuration to recover landing parameters on consent acceptance, configure Google Consent Mode v2 default states properly, enable URL passthrough for ad click IDs, and define an end-to-end verification plan.

---

## 2. Google Consent Mode v2: Default State Configuration

### 2.1 Timing & Placement Requirement
Per official Google Consent Mode documentation, default consent states **must execute before any Google tags (`gtag.js`, GTM container snippet, or GA4 Configuration tag) execute or request network resources**.

### 2.2 Global Site Script / GTM Custom HTML (Head Tag)
Ensure the following script executes synchronously as the very first script inside the `<head>` of `index.html`, immediately preceding the GTM container script:

```html
<!-- Google Consent Mode v2 Default Settings -->
<script>
  window.dataLayer = window.dataLayer || [];
  function gtag(){ dataLayer.push(arguments); }

  // Set default consent state to 'denied' with a 500ms update window
  gtag('consent', 'default', {
    'ad_storage': 'denied',
    'analytics_storage': 'denied',
    'ad_user_data': 'denied',
    'ad_personalization': 'denied',
    'wait_for_update': 500
  });

  // Enable URL Passthrough for ad click parameters (gclid, gbraid, wbraid, etc.)
  gtag('set', 'url_passthrough', true);

  // Enable Ads Data Redaction when ad_storage is denied
  gtag('set', 'ads_data_redaction', true);
</script>
<!-- End Google Consent Mode v2 Default Settings -->

<!-- Google Tag Manager Snippet follows below -->
<script>(function(w,d,s,l,i){w[l]=w[l]||[];w[l].push({'gtm.start':
new Date().getTime(),event:'gtm.js'});var f=d.getElementsByTagName(s)[0],
j=d.createElement(s),dl=l!='dataLayer'?'&l='+l:'';j.async=true;j.src=
'https://www.googletagmanager.com/gtm.js?id='+i+dl;f.parentNode.insertBefore(j,f);
})(window,document,'script','dataLayer','GTM-XXXXXXX');</script>
```

### 2.3 Parameter Rationale
- `wait_for_update: 500`: Instructs Google tags to wait up to 500 milliseconds for the banner tool ("Cookies Yes") to push the cached user consent status before dispatching initial network pings. For returning visitors who previously accepted, this prevents false cookieless hits.
- `url_passthrough: true`: Enables Google tags to automatically append click identifiers (`_gl`, `gclid`, etc.) to outbound internal links and preserve them across pages even when cookies cannot yet be stored.

---

## 3. Landing Link Parameter Persistence & On-Accept Replay

### 3.1 Initial Landing Parameter Capture (`sessionStorage`)
When a user first lands on the website (before interacting with the cookie banner), we must capture their original landing URL, referrer, and UTM parameters so they survive SPA page changes and delayed consent updates.

Create a **Custom HTML Tag** in GTM:
- **Tag Name:** `cHTML - Capture Landing Parameters`
- **Trigger:** `Consent Initialization - All Pages` (or `Initialization - All Pages`)
- **HTML Content:**

```html
<script>
(function() {
  try {
    var storageKey = 'rh_initial_landing';
    // Only capture on first landing within the browsing session
    if (!sessionStorage.getItem(storageKey)) {
      var currentUrl = window.location.href;
      var referrer = document.referrer || '';
      var search = window.location.search;

      // Check if current URL contains tracking parameters or external referrer
      var hasTracking = /[?&](utm_source|utm_medium|utm_campaign|utm_content|utm_term|gclid|gbraid|wbraid|fbclid|msclkid|ttclid|clickref)=/i.test(search);
      
      var landingData = {
        landing_url: currentUrl,
        landing_referrer: referrer,
        has_tracking: hasTracking,
        captured_at: new Date().toISOString()
      };

      sessionStorage.setItem(storageKey, JSON.stringify(landingData));
    }
  } catch (e) {
    // Fail silently if sessionStorage is blocked
  }
})();
</script>
```

---

### 3.2 GTM Custom Variables

Configure the following variables in GTM:

1. **`1st_party_cookie_or_session - Initial Landing URL`**:
   - Variable Type: **Custom JavaScript**
   - Name: `cjs - initial_landing_url`
   - Code:
     ```javascript
     function() {
       try {
         var raw = sessionStorage.getItem('rh_initial_landing');
         if (!raw) return window.location.href;
         var parsed = JSON.parse(raw);
         return parsed.landing_url || window.location.href;
       } catch (e) {
         return window.location.href;
       }
     }
     ```

2. **`cjs - initial_landing_referrer`**:
   - Variable Type: **Custom JavaScript**
   - Name: `cjs - initial_landing_referrer`
   - Code:
     ```javascript
     function() {
       try {
         var raw = sessionStorage.getItem('rh_initial_landing');
         if (!raw) return document.referrer || '';
         var parsed = JSON.parse(raw);
         return parsed.landing_referrer || document.referrer || '';
       } catch (e) {
         return document.referrer || '';
       }
     }
     ```

3. **`dlu - consent_action`**:
   - Variable Type: **Data Layer Variable**
   - Data Layer Variable Name: `consent_status` (or banner-specific event property emitted by Cookies Yes, e.g. `cookie_consent_update`)

---

### 3.3 On-Accept Event Replay Configuration

When the visitor clicks "Accept" on the cookie banner:
1. Cookies Yes issues `gtag('consent', 'update', { 'analytics_storage': 'granted', ... })`.
2. A custom event (e.g. `cookie_consent_update` or `cookie_consent_accepted`) is pushed to `dataLayer`.
3. GTM fires a dedicated GA4 event carrying the original landing URL, ensuring GA4 attributes the newly granted session to the original campaign source.

#### Trigger Configuration:
- **Name:** `Custom Event - Cookie Consent Accepted`
- **Event Type:** Custom Event
- **Event Name:** `cookie_consent_update|cookie_consent_accepted|cookies_accepted` (use regex matching your banner tool's emitted event)
- **Firing Rule:** Fire when `analytics_storage` becomes `'granted'`.

#### Tag Configuration:
- **Tag Type:** **Google Analytics: GA4 Event**
- **Configuration Tag:** None (or your GA4 Measurement ID / Google Tag `G-XXXXXXXXXX`)
- **Event Name:** `page_view`
- **Event Parameters:**
  | Parameter Name | Value | Description |
  | :--- | :--- | :--- |
  | `page_location` | `{{cjs - initial_landing_url}}` | Forces GA4 to parse initial campaign UTMs/click IDs |
  | `page_referrer` | `{{cjs - initial_landing_referrer}}` | Preserves original external referrer |
  | `replayed_on_consent` | `true` | Marker for audit & debugging |
  | `send_page_view` | `true` | Standard GA4 page_view trigger |

---

## 4. Single-Page App (SPA) Route Change Handling

On single-page site navigations (Next.js / React Router / Vanilla SPA router):
1. Configure the primary GA4 Configuration / Google Tag with `send_page_view: false` if virtual page views are handled manually.
2. For subsequent virtual route changes, send `page_view` with:
   - `page_location`: `window.location.href`
   - `page_referrer`: Previous SPA path (e.g., `https://rafflehouse.com/previous-page`)
3. Do **NOT** overwrite `sessionStorage.getItem('rh_initial_landing')` on internal route changes.

---

## 5. End-to-End Test & Verification Plan

Follow this sequential verification checklist before deploying container versions to production.

### Step 1: Query Parameter Preservation on Accept
1. Open an incognito browser window.
2. Navigate to: `https://rafflehouse.com/?utm_source=test&utm_medium=test&utm_campaign=qa_consent_mode`
3. Inspect `sessionStorage.getItem('rh_initial_landing')` in DevTools Console:
   - Verify it contains `utm_source=test` and `utm_medium=test`.
4. Open GA4 **DebugView** (via Google Analytics Debugger Chrome extension).
5. Click **"Accept All"** on the cookie banner.
6. Observe the incoming events in GA4 DebugView:
   - `consent_update` fires with `analytics_storage: granted`.
   - The subsequent `page_view` has `page_location` containing `?utm_source=test&utm_medium=test`.
   - Session campaign attributes show source = `test`, medium = `test`.

### Step 2: Cookieless Pings on Decline
1. Open a fresh incognito browser window.
2. Navigate to: `https://rafflehouse.com/?utm_source=test_decline&utm_medium=test`
3. Click **"Reject All"** (or Decline) on the cookie banner.
4. Verify in DevTools Network tab (filter for `collect?`):
   - Requests to `google-analytics.com/g/collect` include `gcs=G100` (Consent Mode denied) or `gcs=G101`.
   - No cookie header `_ga` or client-side storage is written.
   - Pings carry cookieless parameter `ep.page_location` containing landing UTMs.

### Step 3: Warehouse & Daily Reporting Verification
On the day following test deployment:
1. Run data warehouse reconciliation:
   ```sql
   SELECT
     d,
     orders,
     orders_tracked,
     pct_tracked,
     ga4_sessions,
     consent_granted_share,
     est_visits,
     ga4_sessions_reliable
   FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_tracking_coverage_daily`
   WHERE d = CURRENT_DATE('Europe/London') - 1;
   ```
2. Check `rpt_visit_source_daily`:
   ```sql
   SELECT
     channel,
     consent,
     SUM(landings) AS landings,
     SUM(page_views) AS page_views
   FROM `rh-ga4-491819.rh_curated_Bright_Test.rpt_visit_source_daily`
   WHERE d = CURRENT_DATE('Europe/London') - 1
   GROUP BY 1, 2
   ORDER BY landings DESC;
   ```
3. Confirm that classified landings share exceeds 80% and Unassigned / Direct does not exceed normal baselines.

---

## 6. Strategic Compliance & Policy Decisions for Company Leadership

The following questions involve regulatory, legal, and privacy risk decisions. **These must be decided by Raffle House executive and legal leadership, not by developers:**

1. **Cookieless Modeling vs. Strict Blocking:**
   - Under UK GDPR / PECR, Google Consent Mode v2 sends cookieless pings when consent is denied (no cookie identifier stored on the device, used for Google's machine learning conversion modeling).
   - *Question for Leadership:* Does Raffle House wish to continue using Google's Advanced Consent Mode (transmitting cookieless pings with IP anonymization), or should tags be completely blocked from firing until explicit consent is granted (Basic Consent Mode)?
2. **Behavioral Data Storage on Decline:**
   - *Question for Leadership:* For visitors who explicitly decline all analytics, should `sessionStorage` be used exclusively for transient in-memory attribution if they later change their mind, or should all custom sessionStorage scripts be disabled upon explicit decline?
3. **Google Signals & Enhanced Conversions:**
   - When consent is denied, `ad_personalization` and `ad_user_data` are denied. Ensure no hashed first-party user data (email, phone) is transmitted on form submissions unless explicit consent has been granted.

---

*End of Specification.*
