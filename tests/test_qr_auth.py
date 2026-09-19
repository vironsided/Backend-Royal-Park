"""Регрессия P0: генерация QR-токена больше не анонимна.

Раньше POST /api/qr/users/{id}/qr-token был открыт (роутер подключался без
staff_only), из-за чего любой аноним выпускал одноразовый токен на ЛЮБОЙ user_id
и затем анонимно ставил свой пароль -> захват аккаунта, вплоть до ROOT.
Здесь фиксируем: генерация требует staff-сессии (ROOT/ADMIN), ADMIN не может
выпустить токен на ROOT/другого ADMIN, а анонимный онбординг (/verify +
/change-password) по-прежнему работает.
"""

from app.models import RoleEnum


def _target_with_temp_password(db, factory, username="res1", role=RoleEnum.RESIDENT):
    """Пользователь в состоянии «нужно сменить пароль» — на него можно выпустить QR."""
    u = factory.user(username, role)
    u.require_password_change = True
    u.temp_password_plain = "TmpPass123"
    db.add(u); db.commit(); db.refresh(u)
    return u


def test_qr_generate_requires_auth(client, factory, db_session):
    """Аноним (без кук) -> 401, а не 200 с токеном (закрытый P0-регресс)."""
    target = _target_with_temp_password(db_session, factory)
    resp = client.post(f"/api/qr/users/{target.id}/qr-token")
    assert resp.status_code == 401, resp.text


def test_qr_generate_operator_forbidden(client, factory, db_session):
    """OPERATOR — сотрудник, но управлять пользователями не может -> 403."""
    target = _target_with_temp_password(db_session, factory)
    op = factory.user("op1", RoleEnum.OPERATOR)
    resp = client.post(f"/api/qr/users/{target.id}/qr-token", cookies=factory.cookie(op))
    assert resp.status_code == 403, resp.text


def test_qr_generate_admin_cannot_target_root(client, factory, db_session):
    """ADMIN не должен выпускать токен на ROOT (иначе тот же путь -> захват ROOT)."""
    root = _target_with_temp_password(db_session, factory, username="root1", role=RoleEnum.ROOT)
    admin = factory.user("admin1", RoleEnum.ADMIN)
    resp = client.post(f"/api/qr/users/{root.id}/qr-token", cookies=factory.cookie(admin))
    assert resp.status_code == 403, resp.text


def test_qr_onboarding_flow_admin_then_anonymous(client, factory, db_session):
    """Легальный онбординг цел: ADMIN выпускает токен резиденту (200),
    затем анонимные /verify и /change-password работают, пароль установлен,
    а session_version резидента бампнут (прежние сессии отозваны)."""
    admin = factory.user("admin2", RoleEnum.ADMIN)
    resident = _target_with_temp_password(db_session, factory, username="res2")
    sv_before = resident.session_version or 0

    # 1) staff генерирует токен
    gen = client.post(f"/api/qr/users/{resident.id}/qr-token", cookies=factory.cookie(admin))
    assert gen.status_code == 200, gen.text
    token = gen.json()["token"]

    # 2) аноним проверяет токен (без кук) — префилл-поля отдаются
    ver = client.get(f"/api/qr/verify/{token}")
    assert ver.status_code == 200, ver.text
    assert ver.json()["user_id"] == resident.id

    # 3) аноним ставит пароль
    ch = client.post("/api/qr/change-password", json={
        "token": token,
        "new_password": "NewPass1234",
        "confirm_password": "NewPass1234",
        "full_name": "Test Resident",
        "phone": "+994500000000",
        "email": "res2@example.com",
    })
    assert ch.status_code == 200, ch.text

    db_session.refresh(resident)
    assert resident.require_password_change is False
    assert resident.temp_password_plain is None
    # смена пароля отзывает прежние сессии
    assert (resident.session_version or 0) == sv_before + 1
