# Azericard Full Payment Audit Report

Date: 2026-05-05  
Environment: Production Railway backend + production frontend integration points  
Auditor: Codex agent (automated live/API verification)

## Executive Verdict

- Automated live checks completed: **23 / 23 PASS**.
- Core backend payment contract is healthy: initiate/signature/status/callback validation/redirect/post-auth endpoints.
- Negative-path behavior is correct (missing ORDER, invalid callback signature, fail redirect).
- Wallet initiation contract is correct (Google Pay token forwarding + `TRTYPE=1` for wallet flow).
- Remaining blocker for full cardholder end-to-end success confirmation is external Azericard ACS sandbox instability (CSP + challenge JS error), not merchant backend logic.

## Professional Checklist (Executed)

## 1) Platform health and config
- [x] `GET /healthz` returns healthy response.
- [x] `GET /api/azericard/wallet-config` returns structured payload.
- [x] `google_pay` and `apple_pay` nodes present in response.
- [x] Google Pay runtime gateway config present (`azericardgpay`, TEST env).

Result: **PASS**

## 2) Standard initiate contract (card flow)
- [x] `POST /api/azericard/initiate` returns `ok=true`.
- [x] Gateway URL is `https://testmpi.3dsecure.az/cgi-bin/cgi_link`.
- [x] `P_SIGN` is hex and length 512 (RSA 2048 signature blob).
- [x] `COUNTRY` and `MERCH_GMT` included.
- [x] `TRTYPE` is allowed auth mode (`0` or `1`).
- [x] `order_id` returned.

Result: **PASS**

Evidence order IDs from live run:
- `17204526050530773312`
- `17204526050541711189`

## 3) Signature integrity
- [x] Off-gateway cryptographic verification of generated `P_SIGN` passes with merchant public key.

Result: **PASS**

## 4) Status inquiry contract
- [x] `GET /api/azericard/status/{order_id}?tran_trtype=1` reachable.
- [x] Newly initiated order reports `local_status=INITIATED`.
- [x] `TRAN_TRTYPE` support confirmed on status path.

Result: **PASS**

## 5) Callback negative-path validation
- [x] Callback without `ORDER` is rejected with HTTP 400 (`ORDER is required`).
- [x] Callback with invalid signature is rejected with HTTP 400 (`Invalid callback signature`).
- [x] Local transaction status transitions to `SIGNATURE_FAILED` for bad signature case.

Result: **PASS**

Negative-path evidence order ID:
- `17204526050541711189` (after bad callback, local status became `SIGNATURE_FAILED`)

## 6) Success/fail frontend redirect contract
- [x] `GET /api/azericard/success?...` responds with HTTP 302 to SPA route and `ok=online_payment_success`.
- [x] `GET /api/azericard/fail?...` responds with HTTP 302 to SPA route and `ok=online_payment_failed&reason=...`.

Result: **PASS**

Observed redirect locations:
- Success: `.../user/dashboard.html?ok=online_payment_success&order_id=...#bills`
- Fail: `.../user/dashboard.html?ok=online_payment_failed&order_id=...&reason=declined#bills`

## 7) Post-auth operation endpoints
- [x] `POST /api/azericard/complete` endpoint reachable.
- [x] `POST /api/azericard/reversal?trtype=22` endpoint reachable.
- [x] `POST /api/azericard/reversal?trtype=24` endpoint reachable.
- [x] `POST /api/azericard/operation` supports `trtype=21|22|24`.

Note: dummy order intentionally returns not-found class responses, which confirms route wiring/validation behavior.

Result: **PASS**

## 8) Wallet initiation contract
- [x] Wallet initiate request accepted with `wallet_provider=google_pay`.
- [x] Initiate payload includes `GPAYTOKEN` when token is provided.
- [x] Wallet flow enforces `TRTYPE=1`.

Result: **PASS**

## 9) Frontend behavior expectations (what must / must not appear)

### On payment fail
Must:
- Show exactly one error toast in active UI language.
- Stay in SPA flow (redirect back to dashboard bills tab with query markers).

Must not:
- Duplicate identical toasts from one user action.
- Backend raw HTML terminal page shown to user after fail.

### On payment success
Must:
- Show exactly one success toast in active UI language.
- Return to SPA dashboard bills context.

Current implementation status from deployed code checks:
- Redirect markers and localization keys: **PASS**
- Duplicate toast protections in report-payment flow: **PASS**

## 10) Database-level verification status

Direct SQL verification from this execution environment could not be completed because Railway CLI session is not authenticated (`railway whoami` returns unauthorized token refresh).

Impact:
- I can verify API-observable state transitions (done), but cannot independently query `payments`, `payment_applications`, `payment_logs` tables from this terminal session.

Recommended immediate DB proof queries (run once Railway auth is restored):

```sql
SELECT order_id, gateway_status, payment_id, trtype, created_at
FROM online_transactions
WHERE order_id IN ('17204526050530773312', '17204526050541711189')
ORDER BY created_at DESC;
```

```sql
SELECT id, resident_id, amount_total, method, reference, created_at
FROM payments
WHERE reference IN ('17204526050530773312', '17204526050541711189')
ORDER BY id DESC;
```

```sql
SELECT payment_id, invoice_id, amount_applied, reference, created_at
FROM payment_applications
WHERE payment_id IN (
  SELECT id FROM payments
  WHERE reference IN ('17204526050530773312', '17204526050541711189')
)
ORDER BY id;
```

## 11) External dependency risk noted during manual browser attempts

Observed in Azericard ACS sandbox (external):
- CSP frame-ancestors mismatch (`testacs` vs `testacs1`) causing frame block.
- ACS challenge page JS runtime error (`Cannot read properties of null (reading 'style')`).

These errors originate on ACS sandbox hosts and can block full 3DS challenge completion independently of merchant backend correctness.

## Final Conclusion

Merchant system backend/payment integration is validated and stable for all testable contracts from this environment:
- request signing,
- callback validation,
- local status transitions,
- fail/success redirect behavior,
- wallet request construction,
- operation/status endpoint availability.

For strict 100% closure including SQL proof of successful cardholder 3DS completion rows, only two remaining prerequisites are external/access-related:
1) stable ACS sandbox challenge page from Azericard, and  
2) restored Railway SQL access token in this terminal session.

