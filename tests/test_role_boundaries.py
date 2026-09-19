"""audit res-2: ролевые границы staff-роутеров на СЕРВЕРЕ.

До фикса разделение OPERATOR/SALES существовало только в админке (CSS
`html[data-user-role=...]` + гарды роутов), а на бэкенде все четыре staff-роли
были в одном списке — то есть OPERATOR и SALES по API были равны ROOT и могли,
например, править начальный долг резидента (деньги), сбрасывать пароли жильцов
или менять тарифы в обход интерфейса.

Тесты фиксируют матрицу, которую мы закрепили:
  * SALES  — вообще не имеет доступа к шести биллинговым роутерам
             (в sales.html нет ни одного обращения к ним; ему нужен только
             /api/sales/* и /api/users/me);
  * OPERATOR — читает всё в своих разделах и делает свою ежедневную работу
             (показания, разнесение платежей), но не может создавать/править/
             удалять резидентов, тарифы, счета, аккаунты жильцов и принимать
             платежи — этих кнопок у него в UI и так нет;
  * ROOT/ADMIN — как было, без ограничений.

Проверяем именно факт прохождения авторизации: 403 = запрещено, любой другой
код (404/422/200) = роль допущена до хендлера.
"""
import pytest

import app.main as main_module
from app.models import RoleEnum


NOPE = 999999


@pytest.fixture(autouse=True)
def _isolate_middleware_db(db_session, monkeypatch):
    """Мидлвары в main.py (block_until_password_set и др.) открывают собственный
    SessionLocal() и потому ходят в НАСТОЯЩУЮ базу мимо dependency_overrides.
    Из-за этого тестовый пользователь брался из дев-БД по совпадению id, и
    запрос отбивался 403 password_change_required — то есть тест мог «зеленеть»
    по неверной причине. Подменяем фабрику сессии на тестовую (sqlite in-memory),
    чтобы проверялись именно ролевые границы.
    """
    class _NoCloseSession:
        def __init__(self, s):
            self._s = s

        def __getattr__(self, item):
            return getattr(self._s, item)

        def close(self):  # владелец сессии — фикстура db_session
            pass

    monkeypatch.setattr(main_module, "SessionLocal", lambda: _NoCloseSession(db_session))


@pytest.fixture()
def roles(factory, db_session):
    """Сессионные куки для всех четырёх staff-ролей.

    require_password_change сбрасываем: у свежесозданного пользователя он True по
    умолчанию, и тогда middleware отдаёт 403 password_change_required — тест
    получал бы «правильный» код по неправильной причине.
    """
    out = {}
    for name, role in (
        ("ROOT", RoleEnum.ROOT),
        ("ADMIN", RoleEnum.ADMIN),
        ("OPERATOR", RoleEnum.OPERATOR),
        ("SALES", RoleEnum.SALES),
    ):
        u = factory.user(f"rb_{name.lower()}", role)
        u.require_password_change = False
        db_session.commit()
        out[name] = factory.cookie(u)
    return out


def _call(client, method, path, cookies, json_body=None):
    return client.request(method, path, cookies=cookies, json=json_body)


# Опасная запись: через штатный интерфейс ни OPERATOR, ни SALES её не делают.
DANGEROUS = [
    ("POST", "/api/residents/", {}),
    ("PUT", f"/api/residents/{NOPE}", {}),
    ("DELETE", f"/api/residents/{NOPE}", None),
    ("POST", "/api/tenants", {}),
    ("PUT", f"/api/tenants/{NOPE}", {}),
    ("POST", f"/api/tenants/{NOPE}/reset", None),
    ("DELETE", f"/api/tenants/{NOPE}", None),
    ("POST", "/api/tariffs", {}),
    ("PUT", f"/api/tariffs/{NOPE}", {}),
    ("DELETE", f"/api/tariffs/{NOPE}", None),
    ("POST", "/api/invoices/bulk-issue", {}),
    ("POST", "/api/invoices/bulk-notify", {}),
    ("PUT", f"/api/invoices/{NOPE}", {}),
    ("POST", f"/api/invoices/{NOPE}/cancel", {}),
    ("POST", f"/api/invoices/{NOPE}/reissue", {}),
    ("POST", "/api/payments/", {}),
]

# Штатная работа оператора — ломать нельзя.
OPERATOR_ALLOWED = [
    ("GET", "/api/residents/", None),
    ("GET", f"/api/residents/{NOPE}", None),
    ("GET", "/api/tenants", None),
    ("GET", "/api/tariffs", None),
    ("GET", "/api/readings/", None),
    # ввод показаний — основная ежемесячная работа оператора
    ("POST", "/api/readings/", {"resident_id": NOPE, "date_str": "2026-08-01", "items": []}),
    ("DELETE", f"/api/readings/meter/{NOPE}/last", None),
    ("GET", "/api/invoices", None),
    ("GET", "/api/payments/", None),
    # разнесение платежа по счетам на странице /payment-view
    ("POST", f"/api/payments/{NOPE}/applications", {"items": []}),
    ("POST", f"/api/payments/{NOPE}/auto-apply", {}),
    ("POST", f"/api/payments/{NOPE}/auto-apply-advance", {}),
]

# Всё, что вообще есть в шести биллинговых роутерах, для SALES закрыто.
SALES_FORBIDDEN = DANGEROUS + OPERATOR_ALLOWED


@pytest.mark.parametrize("method,path,body", DANGEROUS)
def test_operator_cannot_perform_dangerous_writes(client, roles, method, path, body):
    r = _call(client, method, path, roles["OPERATOR"], body)
    assert r.status_code == 403, f"{method} {path} -> {r.status_code}"


@pytest.mark.parametrize("method,path,body", OPERATOR_ALLOWED)
def test_operator_daily_work_still_allowed(client, roles, method, path, body):
    r = _call(client, method, path, roles["OPERATOR"], body)
    assert r.status_code != 403, f"{method} {path} -> 403 (сломали работу оператора)"


@pytest.mark.parametrize("method,path,body", SALES_FORBIDDEN)
def test_sales_has_no_access_to_billing_routers(client, roles, method, path, body):
    r = _call(client, method, path, roles["SALES"], body)
    assert r.status_code == 403, f"{method} {path} -> {r.status_code}"


@pytest.mark.parametrize("role", ["ROOT", "ADMIN"])
@pytest.mark.parametrize("method,path,body", DANGEROUS + OPERATOR_ALLOWED)
def test_root_and_admin_unchanged(client, roles, role, method, path, body):
    r = _call(client, method, path, roles[role], body)
    assert r.status_code != 403, f"{role}: {method} {path} -> 403 (регресс)"


def test_sales_keeps_own_section(client, roles):
    """Раздел «Продажи» живёт ВНУТРИ /admin, поэтому SALES обязан сохранить
    доступ к своему API и к профилю — иначе роль теряет смысл."""
    assert client.get("/api/sales/contracts", cookies=roles["SALES"]).status_code != 403
    assert client.get("/api/users/me", cookies=roles["SALES"]).status_code != 403
