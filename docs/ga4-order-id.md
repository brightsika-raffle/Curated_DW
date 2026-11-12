# GA4 Purchase Event Order ID Alignment Specification

**To:** Web Development / Tracking Agency  
**From:** Data Engineering & Analytics  
**Date:** 12 November 2026  
**Subject:** Aligning GA4 `purchase` event identifier with Warehouse `order_id` / `transaction_id`  
**Status:** Request for Implementation  

---

## 1. Problem Statement

Currently, the Google Analytics 4 (GA4) `purchase` event transmits a 36-character UUID as the `transaction_id` (e.g. `d3b07384-d113-46fb-9c98-123456789abc`), whereas the core transactional database and warehouse record orders using a 24-character hexadecimal ID (e.g. `6713840e69123456789abcde`).

An audit of 19–22 October 2026 confirmed:
- **0 exact matches** between GA4 `transaction_id` and warehouse `transaction_id` / `order_id`.
- **0 matches on checkout_id** across both systems.
- GA4 recorded **731 purchases** against **1,871 accepted warehouse orders** (~39.1% capture), but row-level reconciliation is impossible because there is no common join key.

### Consequences:
1. **Inability to audit GA4 purchase drop-off:** We cannot determine which specific orders are missed by GA4 (e.g. ad blocker, browser privacy setting, consent rejection, or webhook vs frontend failure).
2. **Audience segmentation limitations:** We cannot join GA4 prior session history to actual warehouse orders to determine on an order level whether a "new" customer previously visited the site prior to purchase. Instead, we are forced to rely on site-wide aggregate estimates.

---

## 2. Technical Requirement

On the checkout confirmation / thank-you page, update the GA4 `purchase` event payload to pass the internal 24-character warehouse order ID in one of two ways (preferably both):

### Option A (Recommended): Pass as GA4 Standard `transaction_id`
Replace the client-generated 36-character UUID with the 24-character database order ID in the standard `transaction_id` field:

```javascript
window.dataLayer = window.dataLayer || [];
window.dataLayer.push({ ecommerce: null }); // Clear previous ecommerce object
window.dataLayer.push({
  event: "purchase",
  ecommerce: {
    transaction_id: "6713840e69123456789abcde", // 24-char warehouse order ID
    value: 25.00,
    currency: "GBP",
    tax: 0.00,
    shipping: 0.00,
    items: [
      {
        item_id: "TICKETS-25",
        item_name: "25 Ticket Bundle",
        price: 25.00,
        quantity: 1
      }
    ]
  }
});
```

### Option B: Pass as Custom Event Parameter `order_id`
If the 36-character UUID is required by legacy client tags, pass the 24-character warehouse order ID as an explicit custom event parameter named `order_id`:

```javascript
window.dataLayer.push({
  event: "purchase",
  order_id: "6713840e69123456789abcde", // 24-char warehouse order ID
  ecommerce: {
    transaction_id: "d3b07384-d113-46fb-9c98-123456789abc",
    value: 25.00,
    currency: "GBP",
    items: [...]
  }
});
```

---

## 3. Implementation Verification & QA Checklist

1. **Format Check:** Ensure the order ID passed is the exact 24-character string returned by the checkout backend API (case-preserved, typically lower-case hex).
2. **Deduplication:** The `purchase` event must fire exactly once per order confirmation. Refreshing the thank-you page must not fire duplicate purchase events with the same `transaction_id`.
3. **Google Tag Manager:** Ensure the GTM GA4 Purchase tag maps the parameter through to the GA4 config without truncation or sanitisation.
4. **Staging Verification:** Place a test order on staging and verify in Google Tag Assistant / GA4 DebugView that `transaction_id` (or `order_id`) matches the database order record.

---

## 4. Expected Outcome

Once deployed:
1. **1-to-1 Reconciliation:** Warehouse automated DQ tests will verify that every GA4 purchase matches an accepted order in `fact_transactions`.
2. **Order-Level "Visited Before" Attribution:** GA4 cookie history will attach directly to `fact_order_audience_label`, allowing us to classify visitors who browsed for days prior to purchasing directly into the `engaged` audience segment.
