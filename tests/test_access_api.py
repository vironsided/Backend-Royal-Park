from app.models import RoleEnum


def test_ping(client):
    r = client.get("/api/access/ping")
    assert r.status_code == 200 and r.json()["ok"] is True


def _resident_setup(factory):
    b = factory.block(); r = factory.resident(b, unit="A/2")
    u = factory.user("res1", RoleEnum.RESIDENT); factory.link(u, r)
    return u, r


def _guard(factory):
    g = factory.user("guard1", RoleEnum.GUARD)
    return g, factory.cookie(g)


# ---- resident: vehicles ----

def test_resident_adds_and_lists_vehicle(client, factory):
    u, r = _resident_setup(factory); c = factory.cookie(u)
    resp = client.post("/api/access/my/vehicles", json={"plate": "10 aa 123", "label": "BMW"}, cookies=c)
    assert resp.status_code == 201, resp.text
    assert resp.json()["plate"] == "10AA123"
    lst = client.get("/api/access/my/vehicles", cookies=c).json()
    assert len(lst) == 1 and lst[0]["label"] == "BMW"


def test_vehicle_limit_enforced(client, factory, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "ACCESS_VEHICLES_PER_RESIDENT", 2)
    u, r = _resident_setup(factory); c = factory.cookie(u)
    client.post("/api/access/my/vehicles", json={"plate": "AA1"}, cookies=c)
    client.post("/api/access/my/vehicles", json={"plate": "AA2"}, cookies=c)
    third = client.post("/api/access/my/vehicles", json={"plate": "AA3"}, cookies=c)
    assert third.status_code == 400


def test_resident_deletes_own_vehicle(client, factory):
    u, r = _resident_setup(factory); c = factory.cookie(u)
    vid = client.post("/api/access/my/vehicles", json={"plate": "AA1"}, cookies=c).json()["id"]
    assert client.delete(f"/api/access/my/vehicles/{vid}", cookies=c).status_code == 204
    assert client.get("/api/access/my/vehicles", cookies=c).json() == []


# ---- resident: guest passes ----

def test_resident_creates_guest_pass(client, factory):
    u, r = _resident_setup(factory); c = factory.cookie(u)
    body = {"plate": "50xy999", "label": "plumber",
            "valid_from": "2026-06-03T09:00:00", "valid_to": "2026-06-03T18:00:00"}
    resp = client.post("/api/access/my/guest-passes", json=body, cookies=c)
    assert resp.status_code == 201, resp.text
    assert resp.json()["type"] == "GUEST"
    lst = client.get("/api/access/my/guest-passes", cookies=c).json()
    assert len(lst) == 1 and lst[0]["plate"] == "50XY999"


def test_guest_pass_requires_window(client, factory):
    u, r = _resident_setup(factory); c = factory.cookie(u)
    resp = client.post("/api/access/my/guest-passes", json={"plate": "X1"}, cookies=c)
    assert resp.status_code in (400, 422)


# ---- resident: requests ----

def test_resident_creates_and_lists_request(client, factory):
    u, r = _resident_setup(factory); c = factory.cookie(u)
    resp = client.post("/api/access/my/requests",
                       json={"plate": "50xy999", "direction": "IN", "note": "black Mercedes"}, cookies=c)
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "PENDING"
    lst = client.get("/api/access/my/requests", cookies=c).json()
    assert len(lst) == 1 and lst[0]["plate"] == "50XY999" and lst[0]["direction"] == "IN"


# ---- guard: check ----

def test_check_resident_allow(client, factory):
    u, r = _resident_setup(factory); rc = factory.cookie(u)
    client.post("/api/access/my/vehicles", json={"plate": "10AA123"}, cookies=rc)
    g, gc = _guard(factory)
    resp = client.post("/api/access/check", json={"plate": "10 aa 123", "direction": "IN"}, cookies=gc)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["result"] == "ALLOW"
    assert body["house"]


def test_check_unknown(client, factory):
    g, gc = _guard(factory)
    body = client.post("/api/access/check", json={"plate": "ZZ000", "direction": "IN"}, cookies=gc).json()
    assert body["result"] == "UNKNOWN"


def test_check_requires_guard_role(client, factory):
    u, r = _resident_setup(factory); rc = factory.cookie(u)
    resp = client.post("/api/access/check", json={"plate": "AA1", "direction": "IN"}, cookies=rc)
    assert resp.status_code == 403


# ---- guard: open / emergency ----

def test_open_known_logs_manual_open(client, factory):
    u, r = _resident_setup(factory); rc = factory.cookie(u)
    vid = client.post("/api/access/my/vehicles", json={"plate": "10AA123"}, cookies=rc).json()["id"]
    g, gc = _guard(factory)
    resp = client.post("/api/access/open",
                       json={"plate": "10AA123", "direction": "IN", "source": "MANUAL"}, cookies=gc)
    assert resp.status_code == 200 and resp.json()["barrier"]["ok"] is True
    events = client.get("/api/access/events", cookies=gc).json()
    assert events[0]["decision"] == "MANUAL_OPEN" and events[0]["matched_vehicle_id"] == vid


def test_open_unknown_guest_records_visitor(client, factory):
    b = factory.block(); r = factory.resident(b, unit="A/2", owner="Anar")
    g, gc = _guard(factory)
    resp = client.post("/api/access/open", json={
        "plate": "ZZ000", "direction": "IN", "source": "MANUAL",
        "resident_id": r.id, "visitor_name": "Kamil Aliyev", "purpose": "guest"}, cookies=gc)
    assert resp.status_code == 200
    ev = client.get("/api/access/events", cookies=gc).json()[0]
    assert ev["visitor_name"] == "Kamil Aliyev" and ev["resident_id"] == r.id


def test_emergency_open_without_plate(client, factory):
    g, gc = _guard(factory)
    resp = client.post("/api/access/open",
                       json={"direction": "OUT", "source": "MANUAL", "purpose": "emergency"}, cookies=gc)
    assert resp.status_code == 200


# ---- guard: requests inbox ----

def test_guard_lists_and_handles_request(client, factory):
    u, r = _resident_setup(factory); rc = factory.cookie(u)
    rid = client.post("/api/access/my/requests", json={"plate": "50XY999", "direction": "IN"}, cookies=rc).json()["id"]
    g, gc = _guard(factory)
    pending = client.get("/api/access/requests?status=PENDING", cookies=gc).json()
    assert len(pending) == 1 and pending[0]["id"] == rid
    resp = client.post(f"/api/access/requests/{rid}/handle", cookies=gc)
    assert resp.status_code == 200
    mine = client.get("/api/access/my/requests", cookies=rc).json()
    assert mine[0]["status"] == "HANDLED"


def test_guard_rejects_request(client, factory):
    u, r = _resident_setup(factory); rc = factory.cookie(u)
    rid = client.post("/api/access/my/requests", json={"plate": "X", "direction": "OUT"}, cookies=rc).json()["id"]
    g, gc = _guard(factory)
    assert client.post(f"/api/access/requests/{rid}/reject", cookies=gc).status_code == 200
    assert client.get("/api/access/my/requests", cookies=rc).json()[0]["status"] == "REJECTED"


# ---- guard: registry ----

def test_guard_registry_search_and_blacklist(client, factory):
    u, r = _resident_setup(factory); rc = factory.cookie(u)
    client.post("/api/access/my/vehicles", json={"plate": "10AA123", "label": "BMW"}, cookies=rc)
    g, gc = _guard(factory)
    found = client.get("/api/access/vehicles?q=10AA", cookies=gc).json()
    assert len(found) == 1
    vid = found[0]["id"]
    assert client.post(f"/api/access/vehicles/{vid}/blacklist", cookies=gc).status_code == 200
    chk = client.post("/api/access/check", json={"plate": "10AA123", "direction": "IN"}, cookies=gc).json()
    assert chk["result"] == "BLACKLIST"


def test_guard_adds_guest_vehicle(client, factory):
    b = factory.block(); r = factory.resident(b)
    g, gc = _guard(factory)
    body = {"plate": "77ZZ010", "resident_id": r.id, "label": "courier",
            "valid_from": "2026-06-03T09:00:00", "valid_to": "2026-06-03T20:00:00"}
    assert client.post("/api/access/vehicles", json=body, cookies=gc).status_code == 201


# ---- guard: residents search + journal ----

def test_residents_search_returns_house(client, factory):
    # Privacy: search matches block/villa only, never the owner's name, and the
    # owner name is never returned to the guard.
    b = factory.block(name="Block A"); r = factory.resident(b, unit="A/2", owner="Anar Mammadov")
    g, gc = _guard(factory)
    by_name = client.get("/api/access/residents/search?q=Anar", cookies=gc).json()
    assert by_name == []
    res = client.get("/api/access/residents/search?q=A/2", cookies=gc).json()
    assert len(res) == 1
    assert res[0]["house"].startswith("Block A") and res[0]["resident_id"] == r.id
    assert res[0]["owner_name"] is None


def test_events_journal_filters(client, factory):
    g, gc = _guard(factory)
    client.post("/api/access/open", json={"plate": "AA1", "direction": "IN", "source": "MANUAL"}, cookies=gc)
    client.post("/api/access/open", json={"plate": "BB2", "direction": "OUT", "source": "MANUAL"}, cookies=gc)
    allev = client.get("/api/access/events", cookies=gc).json()
    assert len(allev) == 2
    only_in = client.get("/api/access/events?direction=IN", cookies=gc).json()
    assert len(only_in) == 1 and only_in[0]["direction"] == "IN"


# ---- Phase 2: camera/detect + gate-events ----

KEY = {"X-Camera-Key": "rp-camera-dev-key"}


def test_camera_detect_requires_key(client, factory):
    r = client.post("/api/access/camera/detect", data={"direction": "IN", "vehicle_type": "car"})
    assert r.status_code == 401


def test_camera_detect_bad_key(client, factory):
    r = client.post("/api/access/camera/detect", data={"direction": "IN", "vehicle_type": "car"},
                    headers={"X-Camera-Key": "wrong"})
    assert r.status_code == 401


def test_camera_known_plate_auto_opens(client, factory):
    u, r = _resident_setup(factory); rc = factory.cookie(u)
    client.post("/api/access/my/vehicles", json={"plate": "10AA123"}, cookies=rc)
    resp = client.post("/api/access/camera/detect",
                       data={"plate": "10 aa 123", "direction": "IN", "vehicle_type": "car"}, headers=KEY)
    assert resp.status_code == 200, resp.text
    assert resp.json()["action"] == "opened"
    g, gc = _guard(factory)
    ev = client.get("/api/access/events", cookies=gc).json()[0]
    assert ev["decision"] == "AUTO_OPEN" and ev["source"] == "CAMERA"


def test_camera_unknown_plate_queues_gate_event(client, factory):
    resp = client.post("/api/access/camera/detect",
                       data={"plate": "ZZ999", "direction": "IN", "vehicle_type": "car"}, headers=KEY)
    assert resp.json()["action"] == "queued"
    g, gc = _guard(factory)
    ge = client.get("/api/access/gate-events?status=PENDING", cookies=gc).json()
    assert len(ge) == 1 and ge[0]["plate"] == "ZZ999" and ge[0]["hint"] == "UNKNOWN"


def test_camera_no_plate_queues_no_plate(client, factory):
    resp = client.post("/api/access/camera/detect",
                       data={"direction": "IN", "vehicle_type": "motorcycle"}, headers=KEY)
    assert resp.json()["action"] == "queued"
    g, gc = _guard(factory)
    ge = client.get("/api/access/gate-events?status=PENDING", cookies=gc).json()
    assert ge[0]["hint"] == "NO_PLATE" and ge[0]["plate"] in (None, "")


def test_camera_blacklist_queues_alert(client, factory):
    b = factory.block(); r = factory.resident(b)
    g, gc = _guard(factory)
    client.post("/api/access/vehicles", json={"plate": "66KK00", "type": "GUEST", "resident_id": r.id}, cookies=gc)
    vid = client.get("/api/access/vehicles?q=66KK00", cookies=gc).json()[0]["id"]
    client.post(f"/api/access/vehicles/{vid}/blacklist", cookies=gc)
    resp = client.post("/api/access/camera/detect",
                       data={"plate": "66KK00", "direction": "IN", "vehicle_type": "car"}, headers=KEY)
    assert resp.json()["action"] == "queued"
    ge = client.get("/api/access/gate-events?status=PENDING", cookies=gc).json()
    assert ge[0]["hint"] == "BLACKLIST"


def test_guard_resolves_gate_event_open(client, factory):
    b = factory.block(); r = factory.resident(b, owner="Anar")
    client.post("/api/access/camera/detect", data={"plate": "QW12", "direction": "IN", "vehicle_type": "car"}, headers=KEY)
    g, gc = _guard(factory)
    geid = client.get("/api/access/gate-events?status=PENDING", cookies=gc).json()[0]["id"]
    resp = client.post(f"/api/access/gate-events/{geid}/resolve",
                       json={"action": "open", "resident_id": r.id, "visitor_name": "Kamil", "purpose": "guest"}, cookies=gc)
    assert resp.status_code == 200
    assert client.get("/api/access/gate-events?status=PENDING", cookies=gc).json() == []
    ev = client.get("/api/access/events", cookies=gc).json()[0]
    assert ev["visitor_name"] == "Kamil" and ev["resident_id"] == r.id


def test_guard_resolves_gate_event_deny(client, factory):
    client.post("/api/access/camera/detect", data={"plate": "DN1", "direction": "OUT", "vehicle_type": "car"}, headers=KEY)
    g, gc = _guard(factory)
    geid = client.get("/api/access/gate-events?status=PENDING", cookies=gc).json()[0]["id"]
    assert client.post(f"/api/access/gate-events/{geid}/resolve", json={"action": "deny"}, cookies=gc).status_code == 200
    assert client.get("/api/access/gate-events?status=PENDING", cookies=gc).json() == []
