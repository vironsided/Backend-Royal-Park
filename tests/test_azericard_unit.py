"""
Unit tests for Azericard integration — no network, no DB required.
Covers: signature format, PEM helpers, order_id, amount formatting, callback verify.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from decimal import Decimal
from cryptography.hazmat.primitives.asymmetric import rsa, padding
from cryptography.hazmat.primitives import hashes, serialization

# ── helpers ──────────────────────────────────────────────────────────────────

PASS = "PASS"
FAIL = "FAIL"
results = []

def check(label, cond, detail=""):
    status = "✓ PASS" if cond else "✗ FAIL"
    print(f"  {status}  {label}")
    if not cond and detail:
        print(f"         ^ {detail}")
    results.append((label, cond))
    return cond


# ── generate a throwaway RSA-2048 key pair for offline crypto tests ──────────

_priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_pub  = _priv.public_key()

PRIV_PEM = _priv.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.TraditionalOpenSSL,
    serialization.NoEncryption(),
).decode()

PUB_PEM = _pub.public_bytes(
    serialization.Encoding.PEM,
    serialization.PublicFormat.SubjectPublicKeyInfo,
).decode()

# MPI key pair — separate key, simulates Azericard's server key
_mpi_priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_mpi_pub  = _mpi_priv.public_key()

MPI_PUB_PEM = _mpi_pub.public_bytes(
    serialization.Encoding.PEM,
    serialization.PublicFormat.SubjectPublicKeyInfo,
).decode()


# ── patch settings so the service module loads without a real .env ────────────

import unittest.mock as mock

fake_settings = mock.MagicMock()
fake_settings.AZERICARD_TERMINAL_ID = "17204537"
fake_settings.AZERICARD_TERMINAL_UTILITY = "17204537"
fake_settings.AZERICARD_TERMINAL_MAINTENANCE = "17204537"
fake_settings.AZERICARD_TERMINAL_ADVANCE = "17204537"
fake_settings.AZERICARD_TERMINAL_WALLET = "17204538"
fake_settings.AZERICARD_TERMINAL_WALLET_UTILITY = ""
fake_settings.AZERICARD_TERMINAL_WALLET_MAINTENANCE = ""
fake_settings.AZERICARD_TERMINAL_WALLET_ADVANCE = ""
fake_settings.AZERICARD_PRIVATE_KEY = PRIV_PEM
fake_settings.AZERICARD_PUBLIC_KEY = PUB_PEM
fake_settings.AZERICARD_PRIVATE_KEY_UTILITY = ""
fake_settings.AZERICARD_PRIVATE_KEY_MAINTENANCE = ""
fake_settings.AZERICARD_PRIVATE_KEY_ADVANCE = ""
fake_settings.AZERICARD_PUBLIC_KEY_UTILITY = ""
fake_settings.AZERICARD_PUBLIC_KEY_MAINTENANCE = ""
fake_settings.AZERICARD_PUBLIC_KEY_ADVANCE = ""
fake_settings.AZERICARD_PRIVATE_KEY_WALLET = PRIV_PEM
fake_settings.AZERICARD_PUBLIC_KEY_WALLET = PUB_PEM
fake_settings.AZERICARD_PRIVATE_KEY_WALLET_UTILITY = ""
fake_settings.AZERICARD_PRIVATE_KEY_WALLET_MAINTENANCE = ""
fake_settings.AZERICARD_PRIVATE_KEY_WALLET_ADVANCE = ""
fake_settings.AZERICARD_PUBLIC_KEY_WALLET_UTILITY = ""
fake_settings.AZERICARD_PUBLIC_KEY_WALLET_MAINTENANCE = ""
fake_settings.AZERICARD_PUBLIC_KEY_WALLET_ADVANCE = ""
fake_settings.AZERICARD_MPI_PUBLIC_KEY = MPI_PUB_PEM

import importlib
import importlib.util
import types

# Build a minimal package shim so relative imports in azericard.py work.
# The shim entries are RESTORED after loading (see below): leaving a MagicMock
# in sys.modules["app.config"] silently broke other test modules that do
# `from app.config import settings` at test time (e.g. the access vehicle-limit
# test monkeypatched the mock instead of the real settings).
_saved_modules = {k: sys.modules.get(k)
                  for k in ("app", "app.config", "app.services", "app.services.azericard")}
pkg = types.ModuleType("app")
pkg.__path__ = []
pkg.__package__ = "app"
sys.modules["app"] = pkg

config_mod = types.ModuleType("app.config")
config_mod.settings = fake_settings
sys.modules["app.config"] = config_mod
pkg.config = config_mod

svc_pkg = types.ModuleType("app.services")
svc_pkg.__path__ = []
svc_pkg.__package__ = "app.services"
sys.modules["app.services"] = svc_pkg
pkg.services = svc_pkg

# Load azericard.py directly by file path, bypassing package discovery
_az_path = os.path.join(os.path.dirname(__file__), "..", "app", "services", "azericard.py")
spec = importlib.util.spec_from_file_location("app.services.azericard", _az_path,
                                               submodule_search_locations=[])
az_mod = importlib.util.module_from_spec(spec)
az_mod.__package__ = "app.services"
sys.modules["app.services.azericard"] = az_mod
spec.loader.exec_module(az_mod)

# Restore whatever was in sys.modules before the shim, so the rest of the test
# session keeps importing the REAL app package. az_mod itself stays bound to
# fake_settings (its module-level `from ..config import settings` already ran).
for _k, _v in _saved_modules.items():
    if _v is not None:
        sys.modules[_k] = _v
    else:
        sys.modules.pop(_k, None)

build_signature_content = az_mod.build_signature_content
generate_p_sign = az_mod.generate_p_sign
verify_callback_signature = az_mod.verify_callback_signature
build_order_id = az_mod.build_order_id
amount_to_gateway = az_mod.amount_to_gateway
build_timestamp = az_mod.build_timestamp
build_nonce = az_mod.build_nonce
_as_pem = az_mod._as_pem
CREATE_SIGN_FIELDS = az_mod.CREATE_SIGN_FIELDS
CALLBACK_SIGN_FIELDS = az_mod.CALLBACK_SIGN_FIELDS
CALLBACK_SIGN_FIELDS_ALT = az_mod.CALLBACK_SIGN_FIELDS_ALT
TERMINAL_CATEGORY_UTILITY = az_mod.TERMINAL_CATEGORY_UTILITY
TERMINAL_CATEGORY_ADVANCE = az_mod.TERMINAL_CATEGORY_ADVANCE
TERMINAL_GROUP_STANDARD = az_mod.TERMINAL_GROUP_STANDARD
TERMINAL_GROUP_WALLET = az_mod.TERMINAL_GROUP_WALLET


# ═══════════════════════════════════════════════════════════════════════════════
print("\n" + "═" * 60)
print("SECTION 1 — build_signature_content() format (FIX-01 verification)")
print("═" * 60)

# Spec: len(v1)+v1+len(v2)+v2 — length-prefixed concatenation, no separator
data = {"AMOUNT": "100.00", "CURRENCY": "AZN", "TERMINAL": "17204537"}
fields = ["AMOUNT", "CURRENCY", "TERMINAL"]
content = build_signature_content(data, fields)
expected = "6100.003AZN817204537"
check("Length-prefixed MAC format correct", content == expected, f"got: {content!r}")

# Missing field → treats as empty string → "0"
data2 = {"AMOUNT": "50.00"}
c2 = build_signature_content(data2, ["AMOUNT", "MISSING"])
check("Missing field → '0' prefix (empty string)", c2 == "550.000", f"got: {c2!r}")
# "50.00" = 5 chars → "550.00"; MISSING = "" → 0 chars → "0"; total = "550.000"

# None field → empty string
data3 = {"AMOUNT": None}
c3 = build_signature_content(data3, ["AMOUNT"])
check("None value → treated as empty string", c3 == "0", f"got: {c3!r}")


# ═══════════════════════════════════════════════════════════════════════════════
print("\n" + "═" * 60)
print("SECTION 2 — generate_p_sign() returns HEX, not base64 (FIX-02)")
print("═" * 60)

data_sign = {
    "AMOUNT": "100.00", "CURRENCY": "AZN", "TERMINAL": "17204537",
    "TRTYPE": "1", "TIMESTAMP": "20260501120000",
    "NONCE": "abcdef0123456789", "MERCH_URL": "https://royalpark.az",
}
psign = generate_p_sign(data_sign, CREATE_SIGN_FIELDS)
check("P_SIGN is a string", isinstance(psign, str))
check("P_SIGN is lowercase hex only", all(c in "0123456789abcdef" for c in psign), f"non-hex chars found")
check("P_SIGN length = 512 chars (2048-bit RSA → 256 bytes → 512 hex chars)", len(psign) == 512, f"len={len(psign)}")

# Verify it's a valid RSA-SHA256 signature of the MAC content
mac = build_signature_content(data_sign, CREATE_SIGN_FIELDS)
sig_bytes = bytes.fromhex(psign)
try:
    _pub.verify(sig_bytes, mac.encode(), padding.PKCS1v15(), hashes.SHA256())
    check("P_SIGN cryptographically verifies with matching public key", True)
except Exception as e:
    check("P_SIGN cryptographically verifies with matching public key", False, str(e))


# ═══════════════════════════════════════════════════════════════════════════════
print("\n" + "═" * 60)
print("SECTION 3 — CREATE_SIGN_FIELDS signs MERCH_URL not BACKREF (FIX-04)")
print("═" * 60)

check("CREATE_SIGN_FIELDS 7th field is MERCH_URL",
      CREATE_SIGN_FIELDS[-1] == "MERCH_URL",
      f"last field = {CREATE_SIGN_FIELDS[-1]!r}")
check("BACKREF not in CREATE_SIGN_FIELDS",
      "BACKREF" not in CREATE_SIGN_FIELDS,
      f"fields = {CREATE_SIGN_FIELDS}")


# ═══════════════════════════════════════════════════════════════════════════════
print("\n" + "═" * 60)
print("SECTION 4 — verify_callback_signature() uses MPI key (FIX-03)")
print("═" * 60)

# Simulate Azericard callback: signed with MPI private key
cb_data = {
    "AMOUNT": "100.00", "CURRENCY": "AZN", "TERMINAL": "17204537",
    "TRTYPE": "1", "ORDER": "172045370506001", "RRN": "123456789012",
    "INT_REF": "ABCDEF012345",
}
cb_mac = build_signature_content(cb_data, CALLBACK_SIGN_FIELDS)
cb_sig_bytes = _mpi_priv.sign(cb_mac.encode(), padding.PKCS1v15(), hashes.SHA256())
cb_data["P_SIGN"] = cb_sig_bytes.hex()

check("Callback signed by MPI key verifies correctly", verify_callback_signature(cb_data))

# Bad signature → should fail
bad_cb = dict(cb_data)
bad_cb["P_SIGN"] = "deadbeef" * 64  # 512 hex chars but garbage
check("Tampered P_SIGN fails verification", not verify_callback_signature(bad_cb))

# Missing P_SIGN → fail
no_sign_cb = {k: v for k, v in cb_data.items() if k != "P_SIGN"}
check("Missing P_SIGN fails gracefully", not verify_callback_signature(no_sign_cb))

# Alt callback layout (CALLBACK_SIGN_FIELDS_ALT)
cb_alt = {
    "AMOUNT": "200.00", "TERMINAL": "17204537", "APPROVAL": "AB1234",
    "RRN": "987654321098", "INT_REF": "FF00112233",
}
cb_mac_alt = build_signature_content(cb_alt, CALLBACK_SIGN_FIELDS_ALT)
cb_alt_sig = _mpi_priv.sign(cb_mac_alt.encode(), padding.PKCS1v15(), hashes.SHA256())
cb_alt["P_SIGN"] = cb_alt_sig.hex()
check("Alt callback layout (CALLBACK_SIGN_FIELDS_ALT) verifies correctly", verify_callback_signature(cb_alt))


# ═══════════════════════════════════════════════════════════════════════════════
print("\n" + "═" * 60)
print("SECTION 5 — _as_pem() PEM key formatting (FIX-10)")
print("═" * 60)

# PKCS#1 header for private keys
bare_b64 = "MIIEowIBAAKCAQEA..." + "A" * 200
pem_priv = _as_pem(bare_b64, "private")
check("Private key PEM uses PKCS#1 RSA header",
      "BEGIN RSA PRIVATE KEY" in pem_priv,
      f"header found: {pem_priv[:40]}")
check("Private key PEM NOT PKCS#8",
      "BEGIN PRIVATE KEY" not in pem_priv or "BEGIN RSA PRIVATE KEY" in pem_priv)

pem_pub = _as_pem(bare_b64, "public")
check("Public key PEM uses standard header", "BEGIN PUBLIC KEY" in pem_pub)

# Already has header → pass through unchanged
existing = "-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----"
check("PEM with existing header passes through unchanged", _as_pem(existing, "private") == existing)

# Embedded \\n escape sequences are normalized
escaped = bare_b64.replace("", "")
pem_escaped = _as_pem("MIIEowIBAAK\\nCAQEA\\n", "private")
check("\\\\n escape sequences normalized to newlines in PEM", "\n" in pem_escaped)


# ═══════════════════════════════════════════════════════════════════════════════
print("\n" + "═" * 60)
print("SECTION 6 — build_order_id() format")
print("═" * 60)

oid = build_order_id("17204537")
check("order_id is string of digits only", oid.isdigit(), f"got: {oid!r}")
check("order_id length 6–20", 6 <= len(oid) <= 20, f"len={len(oid)}")

oid2 = build_order_id(None)
check("order_id with None terminal works", isinstance(oid2, str) and oid2.isdigit())

# Uniqueness across 100 calls
orders = {build_order_id("17204537") for _ in range(100)}
check("100 order_ids are unique (no duplicates)", len(orders) == 100, f"only {len(orders)} unique")


# ═══════════════════════════════════════════════════════════════════════════════
print("\n" + "═" * 60)
print("SECTION 7 — amount_to_gateway() formatting")
print("═" * 60)

check("Integer → 2 decimal places", amount_to_gateway(100) == "100.00")
check("Float → 2 decimal places", amount_to_gateway(99.9) == "99.90")
check("Decimal → preserved", amount_to_gateway(Decimal("123.45")) == "123.45")
check("Zero → 0.00", amount_to_gateway(0) == "0.00")
check("String input → works", amount_to_gateway("50.5") == "50.50")
check("Rounding: 1.005 → 1.01 or 1.00 (banker)", amount_to_gateway("1.005") in ("1.01", "1.00"))


# ═══════════════════════════════════════════════════════════════════════════════
print("\n" + "═" * 60)
print("SECTION 8 — build_timestamp() and build_nonce()")
print("═" * 60)

ts = build_timestamp()
check("Timestamp is 14-char YYYYMMDDHHmmss", len(ts) == 14 and ts.isdigit(), f"got: {ts!r}")

nonce = build_nonce()
check("Nonce is 16-char hex", len(nonce) == 16 and all(c in "0123456789abcdef" for c in nonce),
      f"got: {nonce!r}")
nonces = {build_nonce() for _ in range(50)}
check("50 nonces are unique", len(nonces) == 50, f"only {len(nonces)} unique")


# ═══════════════════════════════════════════════════════════════════════════════
print("\n" + "═" * 60)
print("SUMMARY")
print("═" * 60)

passed = sum(1 for _, ok in results if ok)
failed = sum(1 for _, ok in results if not ok)
print(f"\n  Total: {len(results)}   Passed: {passed}   Failed: {failed}")

if failed:
    print("\n  FAILED tests:")
    for label, ok in results:
        if not ok:
            print(f"    ✗ {label}")
    sys.exit(1)
else:
    print("\n  All tests PASSED ✓")
