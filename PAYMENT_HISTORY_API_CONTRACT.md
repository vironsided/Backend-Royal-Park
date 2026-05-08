# Resident Payment History API Contract

This document is for mobile integration (Flutter) of resident payment history.

Base path: `/api/resident`
Auth: session cookie (same as existing resident endpoints).

---

## 1) List history

### Endpoint
`GET /api/resident/payment-history`

### Query params
- `page` (int, default `1`)
- `per_page` (int, default `25`, max `100`)
- `date_from` (`YYYY-MM-DD`)
- `date_to` (`YYYY-MM-DD`)
- `status` (string, supports CSV: `confirmed,declined`)
- `operation_type` (string, supports CSV)
- `payment_method` (string, supports CSV)
- `category` (string, supports CSV)
- `resident_id` (int, optional; must belong to current user)
- `q` (free text search by order/reference/invoice/resident/status/type)

### Response shape
```json
{
  "items": [
    {
      "id": "payment:120",
      "source": "payment",
      "source_id": 120,
      "created_at": "2026-05-08T10:17:00+04:00",
      "resident_id": 7,
      "resident_code": "A / 101",
      "resident_label": "Блок A, №101",
      "amount": 15.5,
      "currency": "AZN",
      "status": "confirmed",
      "operation_type": "invoice_payment",
      "payment_method": "bank_card",
      "category": "utility",
      "order_id": "172045370000123",
      "reference": "172045370000123",
      "comment": "AzeriCard online payment (utility)",
      "invoice_id": 830,
      "invoice_number": "INV-7/41/2026-05",
      "invoice_period": "2026-05",
      "applied_total": 15.5,
      "leftover": 0.0
    }
  ],
  "summary": {
    "total_operations": 41,
    "total_amount": 502.2,
    "total_confirmed": 460.7,
    "total_failed": 41.5,
    "total_topup": 110.0,
    "total_invoice_paid": 330.2,
    "total_advance_writeoff": 62.0
  },
  "pagination": {
    "page": 1,
    "per_page": 25,
    "pages": 2,
    "total": 41
  }
}
```

### Notes
- `items` are merged from 3 sources:
  - `payment:*` -> regular/confirmed payment records
  - `tx:*` -> online transaction attempts without local payment (pending/failed)
  - `advance:*` -> advance write-off applications
- Sorted by `created_at desc`.

---

## 2) History detail

### Endpoint
`GET /api/resident/payment-history/{entry_id}`

`entry_id` format:
- `payment:{id}`
- `tx:{id}`
- `advance:{id}`

### 2.1 Payment detail example (`payment:*`)
```json
{
  "id": "payment:120",
  "source": "payment",
  "payment": {
    "id": 120,
    "resident_id": 7,
    "amount_total": 15.5,
    "method": "ONLINE",
    "reference": "172045370000123",
    "comment": "AzeriCard online payment (utility)",
    "received_at": "2026-05-08T10:17:00+04:00",
    "applied_total": 15.5,
    "leftover": 0.0
  },
  "gateway": {
    "order_id": "172045370000123",
    "status": "CONFIRMED",
    "terminal_category": "utility",
    "rrn": "123456789012",
    "int_ref": "ABC123",
    "approval": "000123",
    "action_code": "0",
    "rc": "00"
  },
  "applications": [
    {
      "application_id": 991,
      "invoice_id": 830,
      "invoice_number": "INV-7/41/2026-05",
      "invoice_period": "2026-05",
      "amount_applied": 15.5,
      "reference": "AZERICARD:172045370000123"
    }
  ]
}
```

### 2.2 Transaction detail example (`tx:*`)
```json
{
  "id": "tx:74",
  "source": "online_transaction",
  "transaction": {
    "id": 74,
    "order_id": "172045370000999",
    "resident_id": 7,
    "invoice_id": null,
    "amount_total": 20.0,
    "currency": "AZN",
    "gateway_status": "INITIATED",
    "terminal_category": "advance",
    "trtype": "1",
    "rrn": null,
    "int_ref": null,
    "approval": null,
    "action_code": null,
    "rc": null,
    "created_at": "2026-05-08T11:30:00+04:00",
    "updated_at": "2026-05-08T11:30:02+04:00",
    "wallet_method_guess": "google_pay"
  },
  "request_payload": {},
  "callback_payload": {}
}
```

### 2.3 Advance write-off detail example (`advance:*`)
```json
{
  "id": "advance:351",
  "source": "advance_application",
  "application": {
    "id": 351,
    "payment_id": 96,
    "invoice_id": 830,
    "invoice_number": "INV-7/41/2026-05",
    "amount_applied": 8.25,
    "reference": "ADVANCE:96",
    "created_at": "2026-05-08T09:00:00+04:00"
  },
  "lines": [
    {
      "description": "Electricity",
      "amount_total": 20.0,
      "applied_share": 8.25
    }
  ]
}
```

---

## Enums (normalized fields)

### `status`
- `initiated`
- `confirmed`
- `declined`
- `signature_failed`
- `error`

### `operation_type`
- `invoice_payment`
- `advance_topup`
- `advance_writeoff`
- `manual_adjustment`

### `payment_method`
- `bank_card`
- `apple_pay`
- `google_pay`
- `cash`
- `transfer`
- `advance_internal`
- `unknown`

### `category`
- usually one of: `utility`, `maintenance`, `advance`
- can be `null` for records where category is not available

---

## Error cases

- `401` -> not authenticated / session expired
- `403` -> resident access denied
- `404` -> history entry not found
- `400` -> invalid `entry_id` format for detail endpoint

---

## Quick integration checklist for Flutter

- Use list endpoint with server-side filters + pagination.
- Store `id` exactly as returned (`payment:*`, `tx:*`, `advance:*`).
- On row click, call detail endpoint with this `id`.
- Render chips/badges by normalized enums (`status`, `operation_type`, `payment_method`).
