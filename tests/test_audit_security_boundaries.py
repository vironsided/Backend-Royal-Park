"""Regression coverage for the follow-up authorization/session audit."""

import pytest

from app.config import settings
from app.models import Notification, NotificationStatus, RoleEnum
from app.security import make_session_token


def test_news_public_feed_remains_anonymous(client):
    response = client.get("/api/news/public?limit=100")
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("role", [RoleEnum.RESIDENT, RoleEnum.OPERATOR, RoleEnum.SALES, RoleEnum.GUARD])
def test_news_admin_endpoints_reject_non_admin_roles(client, factory, role):
    actor = factory.user(f"news_{role.value.lower()}", role)
    cookies = factory.cookie(actor)

    assert client.get("/api/news/admin", cookies=cookies).status_code == 403
    assert client.post(
        "/api/news/admin",
        cookies=cookies,
        json={
            "title": {"ru": "x", "az": "x", "en": "x"},
            "content": {"ru": "x", "az": "x", "en": "x"},
        },
    ).status_code == 403
    assert client.get("/api/news/admin/999999", cookies=cookies).status_code == 403
    assert client.put("/api/news/admin/999999", cookies=cookies, json={"priority": 1}).status_code == 403
    assert client.delete("/api/news/admin/999999", cookies=cookies).status_code == 403


@pytest.mark.parametrize("role", [RoleEnum.ROOT, RoleEnum.ADMIN])
def test_news_admin_list_allows_root_and_admin(client, factory, role):
    actor = factory.user(f"news_allowed_{role.value.lower()}", role)
    response = client.get("/api/news/admin", cookies=factory.cookie(actor))
    assert response.status_code == 200, response.text


def test_news_rejects_attribute_breakout_color(client, factory):
    admin = factory.user("news_color_admin", RoleEnum.ADMIN)
    response = client.post(
        "/api/news/admin",
        cookies=factory.cookie(admin),
        json={
            "title": {"ru": "x", "az": "x", "en": "x"},
            "content": {"ru": "x", "az": "x", "en": "x"},
            "icon_color": '#fff" onmouseover="alert(1)',
        },
    )
    assert response.status_code == 422, response.text


def test_operator_can_read_but_cannot_mutate_blocks(client, factory):
    operator = factory.user("blocks_operator", RoleEnum.OPERATOR)
    cookies = factory.cookie(operator)

    assert client.get("/api/blocks/", cookies=cookies).status_code == 200
    assert client.post("/api/blocks/", cookies=cookies, json={"name": "Forbidden"}).status_code == 403
    assert client.put("/api/blocks/999999", cookies=cookies, json={"name": "Forbidden"}).status_code == 403
    assert client.delete("/api/blocks/999999", cookies=cookies).status_code == 403


@pytest.mark.parametrize(
    "role",
    [RoleEnum.ROOT, RoleEnum.ADMIN, RoleEnum.OPERATOR, RoleEnum.SALES, RoleEnum.GUARD],
)
def test_access_my_endpoints_are_resident_only(client, factory, role):
    actor = factory.user(f"access_my_{role.value.lower()}", role)
    cookies = factory.cookie(actor)
    cases = [
        ("GET", "/api/access/my/vehicles", None),
        ("POST", "/api/access/my/vehicles", {"plate": "10-AA-100"}),
        ("DELETE", "/api/access/my/vehicles/999999", None),
        ("GET", "/api/access/my/guest-passes", None),
        (
            "POST",
            "/api/access/my/guest-passes",
            {
                "plate": "10-AA-100",
                "valid_from": "2026-09-21T10:00:00",
                "valid_to": "2026-09-21T11:00:00",
            },
        ),
        ("DELETE", "/api/access/my/guest-passes/999999", None),
        ("GET", "/api/access/my/requests", None),
        ("POST", "/api/access/my/requests", {"plate": "10-AA-100", "direction": "IN"}),
    ]
    for method, path, body in cases:
        response = client.request(method, path, cookies=cookies, json=body)
        assert response.status_code == 403, (method, path, response.text)


def test_access_my_still_allows_resident(client, factory):
    resident = factory.user("access_my_resident", RoleEnum.RESIDENT)
    response = client.get("/api/access/my/vehicles", cookies=factory.cookie(resident))
    assert response.status_code == 200, response.text


@pytest.mark.parametrize(
    "path",
    [
        "/api/azericard/saved-cards",
        "/api/resident/payment-history",
        "/api/resident/advance-history",
    ],
)
def test_revoked_session_version_is_rejected_on_former_raw_session_endpoints(
    client, factory, db_session, path
):
    resident = factory.user("revoked_session_resident", RoleEnum.RESIDENT)
    old_token = make_session_token(resident.id, resident.session_version or 0)

    resident.session_version = (resident.session_version or 0) + 1
    db_session.commit()

    response = client.get(path, cookies={settings.COOKIE_NAME: old_token})
    assert response.status_code == 401, response.text


def test_revoked_session_is_anonymous_on_optional_notification_endpoint(
    client, factory, db_session
):
    resident = factory.user("revoked_optional_resident", RoleEnum.RESIDENT)
    token = make_session_token(resident.id, resident.session_version or 0)
    db_session.add(
        Notification(
            user_id=resident.id,
            message="personal",
            status=NotificationStatus.UNREAD,
            notification_type="CONTRACT_DECISION",
        )
    )
    db_session.commit()

    valid = client.get(
        "/api/notifications/unread-count",
        cookies={settings.COOKIE_NAME: token},
    )
    assert valid.status_code == 200
    assert valid.json()["count"] == 1

    resident.session_version = (resident.session_version or 0) + 1
    db_session.commit()
    revoked = client.get(
        "/api/notifications/unread-count",
        cookies={settings.COOKIE_NAME: token},
    )
    assert revoked.status_code == 200
    assert revoked.json()["count"] == 0
