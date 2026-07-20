"""Cookie-based login gateway for the read-only BTCAlert dashboard."""

from __future__ import annotations

import hmac
import logging
import os
import time
from pathlib import Path

import bcrypt
from flask import Flask, Response, redirect, render_template_string, request, session
from passlib.hash import apr_md5_crypt
from werkzeug.middleware.proxy_fix import ProxyFix


LOGIN_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>BTCAlert 登录</title>
  <style>
    :root { color-scheme: light dark; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
    body { min-height: 100vh; margin: 0; display: grid; place-items: center; background: #f4f6f8; color: #17202a; }
    main { width: min(360px, calc(100vw - 40px)); padding: 28px; border: 1px solid #d9dee3; background: #fff; box-shadow: 0 8px 30px rgb(20 30 40 / 8%); }
    h1 { margin: 0 0 8px; font-size: 24px; letter-spacing: 0; }
    p { margin: 0 0 22px; color: #59636e; line-height: 1.5; }
    label { display: block; margin: 14px 0 6px; font-size: 14px; font-weight: 600; }
    input { box-sizing: border-box; width: 100%; min-height: 42px; padding: 9px 11px; border: 1px solid #b9c1c9; border-radius: 4px; font: inherit; }
    button { width: 100%; min-height: 42px; margin-top: 20px; border: 0; border-radius: 4px; background: #1473e6; color: #fff; font: inherit; font-weight: 700; cursor: pointer; }
    .error { margin: 14px 0 0; color: #b42318; font-size: 14px; }
    @media (prefers-color-scheme: dark) {
      body { background: #11161b; color: #edf2f7; }
      main { background: #1b2229; border-color: #36414b; }
      p { color: #aeb8c2; }
      input { background: #11161b; color: #edf2f7; border-color: #52606d; }
    }
  </style>
</head>
<body>
  <main>
    <h1>BTCAlert</h1>
    <p>登录后查看只读监控状态。</p>
    <form method="post" action="/_login">
      <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
      <label for="username">用户名</label>
      <input id="username" name="username" autocomplete="username" required autofocus>
      <label for="password">密码</label>
      <input id="password" name="password" type="password" autocomplete="current-password" required>
      <button type="submit">登录</button>
      {% if error %}<div class="error" role="alert">{{ error }}</div>{% endif %}
    </form>
  </main>
</body>
</html>
"""


def _read_password_hash(path: str, username: str) -> bytes | None:
    try:
        entries = Path(path).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        logging.getLogger(__name__).exception("Unable to read dashboard credential file")
        return None

    for entry in entries:
        stored_user, separator, stored_hash = entry.partition(":")
        if separator and hmac.compare_digest(stored_user, username):
            return stored_hash.encode("utf-8")
    return None


def _authenticated(ttl_seconds: int) -> bool:
    if session.get("authenticated") is not True:
        return False
    login_time = session.get("login_time")
    return isinstance(login_time, int) and time.time() - login_time <= ttl_seconds


def _verify_password(password: str, password_hash: bytes | None) -> bool:
    if not password_hash:
        return False
    try:
        if password_hash.startswith((b"$2a$", b"$2b$", b"$2y$")):
            return bcrypt.checkpw(password.encode("utf-8"), password_hash)
        if password_hash.startswith(b"$apr1$"):
            return apr_md5_crypt.verify(password, password_hash.decode("utf-8"))
    except (TypeError, ValueError, UnicodeError):
        return False
    return False


def create_app(config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("DASHBOARD_COOKIE_SECRET"),
        HTPASSWD_FILE=os.environ.get(
            "DASHBOARD_HTPASSWD_FILE", "/etc/btc-vol-alert-dashboard.htpasswd"
        ),
        SESSION_TTL_SECONDS=int(os.environ.get("DASHBOARD_SESSION_TTL_SECONDS", "604800")),
        SESSION_COOKIE_NAME="btc_alert_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=int(
            os.environ.get("DASHBOARD_SESSION_TTL_SECONDS", "604800")
        ),
        MAX_CONTENT_LENGTH=16 * 1024,
    )
    if config:
        app.config.update(config)
    if not app.config.get("SECRET_KEY"):
        raise RuntimeError("DASHBOARD_COOKIE_SECRET is required")

    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    @app.after_request
    def secure_response(response: Response) -> Response:
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
            "base-uri 'none'; frame-ancestors 'none'"
        )
        return response

    @app.get("/_health")
    def health() -> tuple[str, int]:
        return "ok\n", 200

    @app.get("/_auth")
    def auth_check() -> tuple[str, int]:
        if _authenticated(app.config["SESSION_TTL_SECONDS"]):
            return "", 204
        return "", 401

    @app.route("/_login", methods=["GET", "POST"])
    def login() -> Response | tuple[str, int]:
        if request.method == "GET":
            if _authenticated(app.config["SESSION_TTL_SECONDS"]):
                return redirect("/", code=302)
            csrf_token = os.urandom(24).hex()
            session["csrf_token"] = csrf_token
            return Response(
                render_template_string(LOGIN_TEMPLATE, csrf_token=csrf_token, error=None),
                mimetype="text/html",
            )

        submitted_csrf = request.form.get("csrf_token", "")
        expected_csrf = session.pop("csrf_token", "")
        if not expected_csrf or not hmac.compare_digest(submitted_csrf, expected_csrf):
            return "Invalid request\n", 400

        username = request.form.get("username", "")[:128]
        password = request.form.get("password", "")[:1024]
        password_hash = _read_password_hash(app.config["HTPASSWD_FILE"], username)
        valid = _verify_password(password, password_hash)

        if not valid:
            app.logger.warning("Dashboard login rejected")
            csrf_token = os.urandom(24).hex()
            session["csrf_token"] = csrf_token
            return Response(
                render_template_string(
                    LOGIN_TEMPLATE,
                    csrf_token=csrf_token,
                    error="用户名或密码不正确。",
                ),
                status=401,
                mimetype="text/html",
            )

        session.clear()
        session.permanent = True
        session["authenticated"] = True
        session["login_time"] = int(time.time())
        app.logger.info("Dashboard login accepted")
        return redirect("/", code=303)

    @app.route("/_logout", methods=["GET", "POST"])
    def logout() -> Response:
        session.clear()
        return redirect("/_login", code=303)

    return app
