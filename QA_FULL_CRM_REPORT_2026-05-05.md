# Full CRM QA Report

Date: 2026-05-05
Environment:
- Frontend: https://frontend-production-5e2f.up.railway.app
- Backend: https://backend-production-9052.up.railway.app

Mode: test-only (no code changes during this run)
Plan reference: Full CRM QA Execution Plan

## Executive Summary

- Total checks executed: 55
- Passed: 40
- Failed: 15
- Verdict: **NO-GO**

Reason for NO-GO: multiple critical RBAC and data exposure defects allow unauthorized or lower-privileged access to sensitive operations and data.

## Test Data Created (staging)

Seed file: `backend_repo/qa_seed_artifacts.json`

- Block: `id=5`
- QA users:
  - ADMIN: `qa_admin_1_05051718fd`
  - OPERATOR: `qa_operator_2_05051718fd`
  - SALES: `qa_sales_3_05051718fd`
  - RESIDENT: `qa_resident_4_05051718fd`
  - RESIDENT: `qa_resident_5_05051718fd`
- Residents:
  - `id=5` (opening debt invoice `id=5`, marked overdue)
  - `id=6` (opening debt invoice `id=6`, due in future/current)
- Tariff created: `id=4`
- Payments created:
  - top-up style payment `id=14`
  - advance-use payment `id=15` with applied amount `30.0`

## Top 5 Blockers

1. **CRITICAL**: Anonymous access to dashboard business data endpoints.
2. **CRITICAL**: OPERATOR can create high-privilege users (ROOT/ADMIN/SALES).
3. **CRITICAL**: RESIDENT can access admin APIs (`/api/users/`, `/api/blocks/`, logs, dashboard stats).
4. **HIGH**: SALES can access non-sales admin APIs (`/api/users/`, `/api/blocks/`).
5. **HIGH**: Role-scoped protections are inconsistent across routers (many endpoints use only `get_current_user` and miss `require_any_role`).

## Findings (ordered by severity)

### 1) CRITICAL — Anonymous data exposure in dashboard API

Endpoints return sensitive aggregate and operational data without authentication:
- `/api/dashboard/stats` -> 200 anonymous
- `/api/dashboard/recent-payments` -> 200 anonymous
- `/api/dashboard/recent-activity` -> 200 anonymous
- `/api/dashboard/payment-chart` -> 200 anonymous

Code evidence (no auth dependency on these handlers):
```74:78:c:\Users\vusal\OneDrive\Desktop\Arxiv\backend_repo\app\routers\api_dashboard.py
@router.get("/stats")
def get_dashboard_stats(
    db: Session = Depends(get_db),
):
```

### 2) CRITICAL — Privilege escalation: OPERATOR can create ROOT/ADMIN/SALES

Reproduction evidence from run:
- `operator create ROOT forbidden => status=201`
- `operator create ADMIN forbidden => status=201`
- `operator create SALES forbidden => status=201`

Code evidence: only ADMIN is restricted; OPERATOR/RESIDENT are not restricted at all.
```106:118:c:\Users\vusal\OneDrive\Desktop\Arxiv\backend_repo\app\routers\api_users.py
@router.post("/", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_user_api(...):
    ...
    if actor.role == RoleEnum.ADMIN and payload.role in (RoleEnum.ROOT, RoleEnum.ADMIN, RoleEnum.SALES):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Недостаточно прав")
```

### 3) CRITICAL — RESIDENT can access admin APIs

Reproduction evidence:
- `resident admin endpoint forbidden /api/users/ => status=200`
- `resident admin endpoint forbidden /api/blocks/ => status=200`
- `resident admin endpoint forbidden /api/dashboard/stats => status=200`
- `resident admin endpoint forbidden /api/logs/payment-logs => status=200`

Impact: resident account can enumerate staff/users and manipulate admin entities (e.g., blocks).

### 4) HIGH — SALES can access non-sales admin APIs

Reproduction evidence:
- `sales access policy /api/users/ => status=200`
- `sales access policy /api/blocks/ => status=200`

Expected: SALES should be scoped to sales routes only.

### 5) MEDIUM — Resident invoice detail endpoint mismatch in QA-created mapping

Reproduction:
- `resident own endpoint /api/resident/invoice/5 => status=403`

Likely cause: resident user to resident linkage rules for newly created entities in this run are stricter than expected. Needs confirmation whether this is intended or data-linking bug.

## Pass Highlights

- Auth bootstrap/login/check/logout API flow works.
- Admin user can access core admin modules.
- Admin restrictions for creating ROOT/ADMIN/SALES are enforced as coded.
- Overdue/current invoice dataset prepared and queryable.
- Payment creation + applications worked on staged data.
- Azericard contract checks passed:
  - `/api/azericard/wallet-config` OK
  - `/api/azericard/initiate` OK
  - `P_SIGN` valid hex length 512
  - `/api/azericard/status/{order}` reachable
  - callback missing ORDER rejected with 400

## Evidence Files

- `backend_repo/qa_seed_artifacts.json`
- `backend_repo/qa_run_results.json`

## Coverage Notes

This run executed API-level and session-level checks comprehensively and validated payment/API behavior with live staging data.
UI deep interaction across every screen in browser (manual visual checks, modals, drag/drop, print layouts) is partially covered by prior checks and should be executed as a dedicated browser session after RBAC blockers are fixed.

## Recommendation

- Release recommendation: **NO-GO** until RBAC and anonymous exposure issues are fixed and re-tested.
- Mandatory re-test scope after fixes:
  1. anonymous dashboard endpoints,
  2. user creation permission matrix,
  3. resident access to admin routes,
  4. sales scope isolation,
  5. full role-based regression from `QA_FULL_TEST_PLAN.md`.
