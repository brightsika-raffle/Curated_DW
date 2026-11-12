# GA4 Purchase Event Order ID Alignment Specification

**To:** Web Development / Tracking Agency  
**From:** Data Engineering & Analytics  
**Date:** 12 November 2026  
**Subject:** Aligning GA4 `purchase` event identifier with platform `transaction_id`  
**Status:** Request for Implementation  

---

## 1. Problem Statement

Currently, the Google Analytics 4 (GA4) `purchase` event transmits a client-generated 36-character UUID as the `transaction_id` (e.g. `d3b07384-d113-46fb-9c98-123456789abc`), whereas the Raffle House platform transactional database and data warehouse record orders using 24-character hexadecimal MongoDB ObjectIds (e.g. `6713840e69123456789abcde`).

Across all historical records:
- There are **0 exact matches** between GA4 `ecommerce.transaction_id` and the platform/warehouse 24-character `transaction_id`.
- GA4 purchase events cannot be joined to backend transaction records to verify tracking parity or attribute order-level outcomes.

### Impact on Analytics & Operations
1. **Purchase Reconciliation:** We cannot identify which specific orders fail to record in GA4 (e.g. ad blockers, consent denial, or client network dropouts).
2. **Audience Attribution:** We cannot determine on an order level whether a customer had prior site browsing sessions before placing their order.

---

## 2. Technical Requirement

On checkout completion (order confirmation / thank-you page), update the data layer event for GA4 `purchase` to pass the 24-character platform MongoDB ObjectId in the standard `transaction_id` field (Option A), or as an explicit custom event parameter `order_id` (Option B).

### Option A (Recommended): Standard GA4 `transaction_id`
Pass the platform's 24-character hexadecimal order identifier directly in the standard `ecommerce.transaction_id` field:

```javascript
window.dataLayer = window.dataLayer || [];
window.dataLayer.push({ ecommerce: null }); // Clear previous ecommerce object
window.dataLayer.push({
  event: "purchase",
  ecommerce: {
    transaction_id: "6713840e69123456789abcde", // 24-character platform MongoDB ObjectId
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

### Option B: Custom Parameter `order_id`
If the 36-character UUID is required by existing client tags, retain it in `transaction_id` and send the 24-character platform order identifier as a top-level parameter named `order_id`:

```javascript
window.dataLayer.push({
  event: "purchase",
  order_id: "6713840e69123456789abcde", // 24-character platform MongoDB ObjectId
  ecommerce: {
    transaction_id: "d3b07384-d113-46fb-9c98-123456789abc",
    value: 25.00,
    currency: "GBP",
    items: [...]
  }
});
```

---

## 3. QA & Verification Checklist

1. **Format:** The identifier must be the exact 24-character string returned by the checkout API (lowercase hexadecimal, 24 characters).
2. **Single Fire:** The `purchase` event must fire exactly once per accepted order. Refreshing the confirmation screen must not fire duplicate purchase events with the same order ID.
3. **Tag Delivery:** Verify in Google Tag Assistant / GA4 DebugView that `transaction_id` (or `order_id`) matches the platform order record.
