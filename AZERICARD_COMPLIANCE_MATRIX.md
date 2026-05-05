# Azericard Compliance Matrix (RoyalPark)

## Scope

This document maps Azericard sandbox requirements to implemented RoyalPark code paths and runtime verification points.

Reference sandbox tools:
- `https://testsite.3dsecure.az/sandbox/p-sign.php`
- `https://testsite.3dsecure.az/sandbox/auth.php`
- `https://testsite.3dsecure.az/sandbox/checkout-reversal-psign.php`
- `https://testsite.3dsecure.az/sandbox/checkout-reversal.php`
- `https://testsite.3dsecure.az/sandbox/transaction_status_psign.php`
- `https://testsite.3dsecure.az/sandbox/transaction_status.php`

## Requirement -> Implementation

1) Auth request includes required fields (`AMOUNT`, `CURRENCY`, `ORDER`, `TERMINAL`, `TRTYPE`, `TIMESTAMP`, `NONCE`, `BACKREF`, `COUNTRY`, `MERCH_GMT`)
- Code: `app/routers/api_azericard.py` -> `_build_gateway_params()`
- Runtime check: `test_payment_live.py` TEST 3

2) Auth `P_SIGN` generated with Azericard field order and RSA-SHA256 hex
- Code: `app/services/azericard.py` -> `CREATE_SIGN_FIELDS`, `build_signature_content()`, `generate_p_sign()`
- Runtime check: `test_payment_live.py` TEST 4 + sandbox `p-sign.php`

3) Callback signature verification with MPI public key
- Code: `app/services/azericard.py` -> `verify_callback_signature()`
- Supports both callback field layouts:
  - `AMOUNT,CURRENCY,TERMINAL,TRTYPE,ORDER,RRN,INT_REF`
  - `AMOUNT,TERMINAL,APPROVAL,RRN,INT_REF`
- Runtime check: successful callback sets `online_transactions.gateway_status=CONFIRMED`

4) Wallet fields for Google Pay / Apple Pay
- Code: `app/routers/api_azericard.py` -> `_build_gateway_params()`
- Fields: `GPAYTOKEN`, `EXT_MPI_ECI`, `TAVV`
- Runtime config endpoint: `GET /api/azericard/wallet-config`

5) TRTYPE coverage
- `TRTYPE=0/1` (auth mode): configurable via env `AZERICARD_AUTH_TRTYPE`
- `TRTYPE=21` (completion): `POST /api/azericard/complete`
- `TRTYPE=22|24` (reversal): `POST /api/azericard/reversal?trtype=22|24`
- Generic post-auth operation: `POST /api/azericard/operation` with body `trtype`
- Code: `app/routers/api_azericard.py`

6) TRTYPE=90 status inquiry + optional `TRAN_TRTYPE`
- Code: `GET /api/azericard/status/{order_id}?tran_trtype=...`
- Default env: `AZERICARD_STATUS_TRAN_TRTYPE`
- `P_SIGN` fields are dynamic and include `TRAN_TRTYPE` when present

## Open profile-sensitive note

- `ORDER` max length differs across Azericard docs (20 vs 32 in different materials).
- Current implementation is capped to `<=20` for compatibility with the active merchant profile:
  - Code: `app/services/azericard.py` -> `build_order_id()`
- Change only with explicit confirmation from Azericard for your exact terminal profile.

## End-to-end PASS criteria

- `online_transactions` transitions `INITIATED -> CONFIRMED` for successful callbacks.
- No false `SIGNATURE_FAILED` on valid callbacks.
- Exactly one `payments` row created per successful order.
- Correct `payment_applications` and invoice status changes.
- Advance top-up (`invoice_id=null`) does not auto-apply to invoices.
- Advance spend uses single application per click (duplicate submit guarded).
