# Azericard Compliance Report (Implementation Evidence)

## Result

Status: **Implemented with profile-safe defaults**.

The integration now covers the requested sandbox operation types and adds configuration/verification points needed for controlled compliance testing.

## Implemented changes

1. Added configurable auth mode (`TRTYPE=0|1`)
- File: `app/config.py`
- Variables:
  - `AZERICARD_AUTH_TRTYPE` (`1` default)
  - `AZERICARD_STATUS_TRAN_TRTYPE` (`1` default)
- Runtime use:
  - `app/routers/api_azericard.py` -> `_resolve_auth_trtype()`
  - `_build_gateway_params()` uses configured auth `TRTYPE`.

2. Added `TRAN_TRTYPE` support for `TRTYPE=90` status inquiry
- File: `app/routers/api_azericard.py`
- Endpoint:
  - `GET /api/azericard/status/{order_id}?tran_trtype=1`
- Behavior:
  - Includes `TRAN_TRTYPE` when query or env value is present.
  - Adds `TRAN_TRTYPE` to signed fields used for `P_SIGN`.

3. Added explicit reversal operation coverage (`TRTYPE=22`, `TRTYPE=24`)
- File: `app/routers/api_azericard.py`
- Endpoints:
  - `POST /api/azericard/reversal?trtype=22|24`
  - `POST /api/azericard/operation` (generic `trtype=21|22|24`)
- Existing completion endpoint kept:
  - `POST /api/azericard/complete` (`TRTYPE=21`)

4. Updated validation/test harness and docs
- File: `test_payment_live.py`
  - Checks auth mode `TRTYPE` in `{0,1}`
  - Calls status with `tran_trtype=1`
  - Verifies operation endpoint availability (`21/22/24`)
- File: `.env.example`
  - Added new Azericard config vars.
- File: `README.md`
  - Documented auth-mode, status `TRAN_TRTYPE`, and post-auth endpoints.

5. Added explicit compliance artifacts
- `AZERICARD_COMPLIANCE_MATRIX.md`
- `AZERICARD_E2E_CHECKLIST.md`

## Compliance notes (important)

1. `ORDER` length ambiguity in Azericard materials
- Some docs mention up to 32; active merchant profile previously required strict behavior and currently uses safer 20-char cap.
- Current behavior intentionally remains unchanged (`build_order_id()` in `app/services/azericard.py`).

2. `TRTYPE=0` preauth should be enabled only when your terminal profile allows it
- Default remains `TRTYPE=1` for production safety.
- Switch only via env (`AZERICARD_AUTH_TRTYPE=0`) after confirmation from Azericard.

## How to verify quickly

1. Run `python test_payment_live.py`.
2. Execute sandbox manual checks from `AZERICARD_E2E_CHECKLIST.md`.
3. Compare DB state (`online_transactions`, `payments`, `payment_applications`, `payment_logs`) per `ORDER`.

If all checklist sections pass, the integration is operationally compliant for your configured profile.
