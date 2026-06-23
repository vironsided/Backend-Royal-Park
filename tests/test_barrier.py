import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.services.barrier import open_barrier


def test_open_barrier_returns_ok_with_echo():
    r = open_barrier(gate="MAIN", direction="IN")
    assert r["ok"] is True
    assert r["gate"] == "MAIN"
    assert r["direction"] == "IN"
