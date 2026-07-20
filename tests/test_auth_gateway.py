from pathlib import Path

import bcrypt
import pytest
from passlib.hash import apr_md5_crypt

from auth_gateway import create_app


@pytest.fixture()
def client(tmp_path: Path):
    password_file = tmp_path / "dashboard.htpasswd"
    password_hash = bcrypt.hashpw(b"correct-password", bcrypt.gensalt(rounds=4))
    password_file.write_bytes(b"btcadmin:" + password_hash + b"\n")
    app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-only-secret",
            "HTPASSWD_FILE": str(password_file),
            "SESSION_COOKIE_SECURE": False,
            "SESSION_TTL_SECONDS": 3600,
        }
    )
    return app.test_client()


def _csrf(client) -> str:
    response = client.get("/_login")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    marker = 'name="csrf_token" value="'
    return body.split(marker, 1)[1].split('"', 1)[0]


def test_login_and_logout_flow(client):
    assert client.get("/_auth").status_code == 401

    response = client.post(
        "/_login",
        data={
            "csrf_token": _csrf(client),
            "username": "btcadmin",
            "password": "correct-password",
        },
    )
    assert response.status_code == 303
    assert response.headers["Location"] == "/"
    assert client.get("/_auth").status_code == 204

    assert client.post("/_logout").status_code == 303
    assert client.get("/_auth").status_code == 401


def test_bad_password_does_not_authenticate(client):
    response = client.post(
        "/_login",
        data={
            "csrf_token": _csrf(client),
            "username": "btcadmin",
            "password": "wrong-password",
        },
    )
    assert response.status_code == 401
    assert client.get("/_auth").status_code == 401


def test_apr1_htpasswd_is_supported(tmp_path: Path):
    password_file = tmp_path / "dashboard.htpasswd"
    password_file.write_text(
        "btcadmin:" + apr_md5_crypt.hash("apr1-password") + "\n", encoding="utf-8"
    )
    app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-only-secret",
            "HTPASSWD_FILE": str(password_file),
            "SESSION_COOKIE_SECURE": False,
        }
    )
    client = app.test_client()
    response = client.post(
        "/_login",
        data={
            "csrf_token": _csrf(client),
            "username": "btcadmin",
            "password": "apr1-password",
        },
    )
    assert response.status_code == 303
    assert client.get("/_auth").status_code == 204


def test_csrf_is_required(client):
    response = client.post(
        "/_login", data={"username": "btcadmin", "password": "correct-password"}
    )
    assert response.status_code == 400


def test_secret_is_required(monkeypatch):
    monkeypatch.delenv("DASHBOARD_COOKIE_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="DASHBOARD_COOKIE_SECRET"):
        create_app()
