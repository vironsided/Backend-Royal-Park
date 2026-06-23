import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from datetime import datetime, timedelta
from app.services.access_logic import decide_access
from app.models import AccessVehicle, VehicleType, VehicleStatus

NOW = datetime(2026, 6, 3, 12, 0, 0)

def _v(**kw):
    base = dict(plate="10AA123", type=VehicleType.RESIDENT, status=VehicleStatus.ACTIVE,
                valid_from=None, valid_to=None)
    base.update(kw); return AccessVehicle(**base)

def test_unknown_when_no_vehicle():
    assert decide_access(None, NOW).result == "UNKNOWN"

def test_resident_active_is_allowed():
    assert decide_access(_v(), NOW).result == "ALLOW"

def test_blacklist_alerts():
    assert decide_access(_v(status=VehicleStatus.BLACKLIST), NOW).result == "BLACKLIST"

def test_guest_inside_window_allowed():
    v = _v(type=VehicleType.GUEST, valid_from=NOW - timedelta(hours=1), valid_to=NOW + timedelta(hours=1))
    assert decide_access(v, NOW).result == "ALLOW"

def test_guest_outside_window_unknown():
    v = _v(type=VehicleType.GUEST, valid_from=NOW + timedelta(hours=1), valid_to=NOW + timedelta(hours=2))
    assert decide_access(v, NOW).result == "UNKNOWN"

def test_guest_expired_unknown():
    v = _v(type=VehicleType.GUEST, valid_from=NOW - timedelta(hours=3), valid_to=NOW - timedelta(hours=1))
    assert decide_access(v, NOW).result == "UNKNOWN"
