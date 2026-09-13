from __future__ import annotations

import os

os.environ.setdefault("SCHEDULER_ENABLED", "false")
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.pop("TELEGRAM_BOT_TOKEN", None)
os.environ.pop("TELEGRAM_CHAT_ID", None)

from datetime import date  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.database import Base, get_db, make_engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Credential, CredentialKind  # noqa: E402

TODAY = date(2026, 9, 13)


@pytest.fixture
def db_session():
    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    testing_session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = testing_session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def client(db_session):
    def _override():
        yield db_session

    app.dependency_overrides[get_db] = _override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def make_credential(db, *, name="Test key", expiry: date, **kwargs) -> Credential:
    cred = Credential(
        name=name,
        provider=kwargs.pop("provider", "TestProvider"),
        environment=kwargs.pop("environment", "production"),
        owner=kwargs.pop("owner", "team"),
        kind=kwargs.pop("kind", CredentialKind.api_credential),
        expiry_date=expiry,
        **kwargs,
    )
    db.add(cred)
    db.commit()
    db.refresh(cred)
    return cred


class FakeNotifier:
    channel = "telegram"

    def __init__(self, ok: bool = True):
        self.ok = ok
        self.messages: list[str] = []

    def send(self, text: str) -> bool:
        self.messages.append(text)
        return self.ok
