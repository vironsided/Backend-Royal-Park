import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

from app.database import Base, get_db
import app.main as main_module
from app.models import User, Resident, Block, RoleEnum, user_residents
from app.security import hash_password, make_session_token
from app.config import settings


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def client(db_session):
    app = main_module.app

    def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


def _make_user(db, username, role, full_name=None):
    u = User(username=username, password_hash=hash_password("Password123!"),
             role=role, full_name=full_name or username, is_active=True)
    db.add(u); db.commit(); db.refresh(u)
    return u


@pytest.fixture()
def factory(db_session):
    """Helpers to build domain rows + an auth cookie for a user."""
    class F:
        def block(self, name="Block A"):
            b = Block(name=name, is_active=True); db_session.add(b); db_session.commit(); db_session.refresh(b); return b
        def resident(self, block, unit="A/2", owner="Anar Mammadov"):
            r = Resident(block_id=block.id, unit_number=unit, owner_full_name=owner)
            db_session.add(r); db_session.commit(); db_session.refresh(r); return r
        def user(self, username, role, full_name=None):
            return _make_user(db_session, username, role, full_name)
        def link(self, user, resident):
            db_session.execute(user_residents.insert().values(user_id=user.id, resident_id=resident.id))
            db_session.commit()
        def cookie(self, user):
            return {settings.COOKIE_NAME: make_session_token(user.id)}
    return F()
