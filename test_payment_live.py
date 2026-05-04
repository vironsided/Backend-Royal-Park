#!/usr/bin/env python3
"""
Live Azericard payment test harness.
Run after Railway deploy + env vars are set.
Usage: python3 test_payment_live.py
"""
import json, sys
import urllib.request, urllib.error

BACKEND = "https://backend-production-9052.up.railway.app"

def get(path):
    req = urllib.request.Request(f"{BACKEND}{path}")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"error": e.code, "body": e.read().decode()}

def post(path, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(f"{BACKEND}{path}", data=data,
                                  headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"error": e.code, "body": e.read().decode()}

def check(label, cond, detail=""):
    status = "✓ PASS" if cond else "✗ FAIL"
    print(f"  {status}  {label}")
    if not cond and detail:
        print(f"         {detail}")
    return cond

print("=" * 60)
print("TEST 1: Health check")
print("=" * 60)
r = get("/healthz")
check("Backend alive", r.get("ok") == True)

print()
print("=" * 60)
print("TEST 2: wallet-config (checks env vars are loaded)")
print("=" * 60)
r = get("/api/azericard/wallet-config")
check("Response ok", r.get("ok") == True)
gpay = r.get("google_pay", {})
apay = r.get("apple_pay", {})
check("google_pay key present", "google_pay" in r)
check("apple_pay key present", "apple_pay" in r, f"missing apple_pay in response")
check("GPay environment is TEST", gpay.get("environment") == "TEST")
check("GPay gateway = azericardgpay", gpay.get("gateway") == "azericardgpay")
print(f"  INFO  GPay supported: {gpay.get('supported')}")
print(f"  INFO  APay supported: {apay.get('supported')}")

print()
print("=" * 60)
print("TEST 3: Initiate standard payment (resident_id=1, 1.00 AZN)")
print("=" * 60)
r = post("/api/azericard/initiate", {"resident_id": 1, "amount": 1.00})

if "error" in r:
    print(f"  ✗ FAIL  initiate returned error {r['error']}: {r.get('body','')[:200]}")
    sys.exit(1)

check("Response ok", r.get("ok") == True, str(r))
check("gateway_url correct", r.get("gateway_url") == "https://testmpi.3dsecure.az/cgi-bin/cgi_link",
      f"got: {r.get('gateway_url')}")

params = r.get("params", {})
p_sign = params.get("P_SIGN", "")
check("P_SIGN present", bool(p_sign))
check("P_SIGN is hex (not base64)", 
      all(c in "0123456789abcdef" for c in p_sign.lower()),
      f"P_SIGN: {p_sign[:40]}...")
check("P_SIGN length = 512 chars (2048-bit RSA)", len(p_sign) == 512, f"got len={len(p_sign)}")
check("TERMINAL is numeric test terminal", params.get("TERMINAL") in ("17204537", "17204538"),
      f"TERMINAL={params.get('TERMINAL')}")
check("MERCH_URL in params", bool(params.get("MERCH_URL")), f"MERCH_URL={params.get('MERCH_URL')}")
check("COUNTRY = AZ", params.get("COUNTRY") == "AZ", f"COUNTRY={params.get('COUNTRY')}")
check("MERCH_GMT set", bool(params.get("MERCH_GMT")), f"MERCH_GMT={params.get('MERCH_GMT')}")
check("TRTYPE = 0 (standard auth)", params.get("TRTYPE") == "0", f"TRTYPE={params.get('TRTYPE')}")

order_id = r.get("order_id", "")
check("order_id present", bool(order_id))

print(f"\n  INFO  order_id: {order_id}")
print(f"  INFO  gateway: {r.get('gateway_url')}")
print(f"  INFO  P_SIGN (first 32): {p_sign[:32]}...")

# Verify P_SIGN cryptographically
print()
print("=" * 60)
print("TEST 4: Cryptographic P_SIGN verification (off-gateway)")
print("=" * 60)
try:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    MERCHANT_PUB = b"""-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAydnI80c/hd72G7LxPwoh
9VMfqWx3j8sXKI3lI9hrNflkRBgUP7V6T5T1Gc6/LZ1t2ESFLseHNYSEaX/E9u/h
p/efrIOHIpmVt2vcvY8mmYwycRxGoSH9onXogzpMYup0RL+QowXizqKgB/bJRo4a
3NxnCppH01EIZFqP+4ebtw0hAwAM3LL89Hm6nS6K/OEBlA8m3yNfglXc6eWWZZA4
ump5XjsdFkgrQTFvlH5fkReemSJotOCnRUCfm+7t+032qCCN6ZMmHC+UVli4hRhL
G01v+mhr5bXUUsqLz8uF5fXjc6q88GGiOQQDAeR3jMNyk3mMp3bRf56gC33GyA8H
vQIDAQAB
-----END PUBLIC KEY-----"""

    CREATE_FIELDS = ["AMOUNT", "CURRENCY", "TERMINAL", "TRTYPE", "TIMESTAMP", "NONCE", "MERCH_URL"]
    content = "".join(f"{len(str(params.get(f,'') or ''))}{str(params.get(f,'') or '')}" for f in CREATE_FIELDS)
    sig = bytes.fromhex(p_sign)
    pub = load_pem_public_key(MERCHANT_PUB)
    pub.verify(sig, content.encode(), padding.PKCS1v15(), hashes.SHA256())
    check("P_SIGN verifies with merchant public key", True)
    print(f"  INFO  MAC: {content[:80]}...")
except ImportError:
    print("  SKIP  cryptography library not available for local verify")
except Exception as e:
    check("P_SIGN verifies with merchant public key", False, str(e))

print()
print("=" * 60)
print("TEST 5: Status check for initiated order")
print("=" * 60)
if order_id:
    r2 = get(f"/api/azericard/status/{order_id}")
    check("Status endpoint reachable", "local_status" in r2, str(r2))
    check("Local status = INITIATED", r2.get("local_status") == "INITIATED",
          f"status={r2.get('local_status')}")

print()
print("=" * 60)
print("SUMMARY")
print("=" * 60)
print("""
The initiate response above contains valid signed gateway params.
To complete a real payment test:

  1. Open a browser and POST to Azericard:
     https://testmpi.3dsecure.az/cgi-bin/cgi_link
     (use the params dict from TEST 3)

  2. Use test card: 5522 0993 1327 8830
     EXP: 06/30  CVV: 669  OTP: 1111

  3. Azericard will POST the callback to:
     https://backend-production-9052.up.railway.app/api/azericard/callback

  4. Check order status at:
     https://backend-production-9052.up.railway.app/api/azericard/status/<order_id>

  Expected: local_status changes from INITIATED → CONFIRMED
""")
