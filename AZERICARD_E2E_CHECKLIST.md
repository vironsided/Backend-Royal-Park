# Azericard E2E Validation Checklist

## 1) Environment and deployment

1. Verify backend is healthy: `GET /healthz`.
2. Verify wallet runtime config: `GET /api/azericard/wallet-config`.
3. Verify env values in Railway:
   - `AZERICARD_CALLBACK_URL`, `AZERICARD_SUCCESS_URL`, `AZERICARD_FAIL_URL`
   - terminal and key variables (standard + wallet)
   - `AZERICARD_AUTH_TRTYPE`
   - `AZERICARD_STATUS_TRAN_TRTYPE`

PASS:
- health endpoint returns `{"ok": true}`
- wallet-config returns structured payload

## 2) Signature and request format

1. Run `python test_payment_live.py`.
2. Confirm:
   - `P_SIGN` is hex
   - auth `TRTYPE` is `0` or `1`
   - `COUNTRY`, `MERCH_GMT` are present
3. Cross-check sample `P_SIGN` on sandbox `p-sign.php`.

PASS:
- all cryptographic checks pass

## 3) Card payment flow (invoice payment)

UI path:
- `Hesablarım` -> open invoice -> `Ödə` -> choose `Bank Card`

Verify in DB:
- `online_transactions.gateway_status = CONFIRMED`
- `payments.reference = ORDER`
- `payment_applications` has rows for target invoice

PASS:
- invoice remaining decreases by paid amount

## 4) Advance top-up flow

UI path:
- `Hesabı ödəmək` -> `Popolnit avans` -> card payment

Verify:
- payment exists in `payments` (method ONLINE/CARD)
- no new `payment_applications` from this top-up
- advance balance increases

PASS:
- funds stay in advance pool

## 5) Advance spend flow

UI path:
- invoice payment page -> method `Royal Park Advance` -> custom amount -> `Ödə`

Verify:
- single success notification
- single `ADVANCE_USE` operation in logs/payments
- advance reduced exactly once

PASS:
- no duplicate deduction on one click

## 6) TRTYPE post-auth operations

API checks:
- `POST /api/azericard/complete` (`TRTYPE=21`)
- `POST /api/azericard/reversal?trtype=22`
- `POST /api/azericard/reversal?trtype=24`
- `POST /api/azericard/operation` with `trtype=21|22|24`

PASS:
- endpoints respond with gateway result (or `Order not found` for dummy order)

## 7) Status inquiry

API check:
- `GET /api/azericard/status/{order_id}?tran_trtype=1`

PASS:
- request completes and local status is returned

## 8) Manual DB queries (Railway Postgres)

Use SQL tab and validate by `ORDER`:

```sql
SELECT order_id, gateway_status, payment_id, trtype, created_at
FROM online_transactions
WHERE order_id = :order_id;
```

```sql
SELECT id, resident_id, amount_total, method, reference, created_at
FROM payments
WHERE reference = :order_id
ORDER BY id DESC;
```

```sql
SELECT payment_id, invoice_id, amount_applied, reference, created_at
FROM payment_applications
WHERE payment_id = :payment_id
ORDER BY id;
```

```sql
SELECT id, action, amount, details, created_at
FROM payment_logs
WHERE payment_id = :payment_id
ORDER BY id DESC;
```
