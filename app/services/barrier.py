import logging

log = logging.getLogger("access.barrier")


def open_barrier(gate: str = "MAIN", direction: str = "IN") -> dict:
    """Phase 1: stub. Logs the open command and returns success.
    Phase 3: replace the body to pulse a relay wired in parallel with the CAME
    control board's manual-open contact (~1s). This is the ONLY place hardware
    integration changes."""
    log.info("BARRIER OPEN gate=%s direction=%s (stub)", gate, direction)
    return {"ok": True, "gate": gate, "direction": direction, "mode": "stub"}
