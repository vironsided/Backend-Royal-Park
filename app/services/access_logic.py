from dataclasses import dataclass
from datetime import datetime
from .. import models


@dataclass
class AccessDecision:
    result: str          # "ALLOW" | "UNKNOWN" | "BLACKLIST"
    reason: str = ""


def decide_access(vehicle, now: datetime) -> AccessDecision:
    """Pure access decision. `vehicle` is an AccessVehicle or None.
    Reused by the guard /check endpoint AND the Phase-2 camera."""
    if vehicle is None:
        return AccessDecision("UNKNOWN", "not_found")
    if vehicle.status == models.VehicleStatus.BLACKLIST:
        return AccessDecision("BLACKLIST", "blacklisted")
    if vehicle.status != models.VehicleStatus.ACTIVE:
        return AccessDecision("UNKNOWN", "inactive")
    if vehicle.type == models.VehicleType.GUEST:
        if vehicle.valid_from and now < vehicle.valid_from:
            return AccessDecision("UNKNOWN", "not_yet_valid")
        if vehicle.valid_to and now > vehicle.valid_to:
            return AccessDecision("UNKNOWN", "expired")
    return AccessDecision("ALLOW", "ok")
