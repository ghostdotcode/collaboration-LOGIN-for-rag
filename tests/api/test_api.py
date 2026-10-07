"""HTTP-level tests: auth, validation, rate limiting, SSE streaming, headers."""

import json
import time
import uuid
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.db

import main  # noqa: E402  (after conftest sets env)
from auth import ALGORITHM, JWT_SECRET  # noqa: E402
from database import SessionLocal  # noqa: E402
from models import User  # noqa: E402

PREFIX = "pytest-"
GOOD_PW = "Str0ng-pass!"


@pytest.fixture(scope="module", autouse=True)
def _cleanup():
    yield
    db = SessionLocal()
    db.query(User).filter(User.email.like(f"{PREFIX}%")).delete(synchronize_session=False)
    db.commit()
    db.close()


@pytest.fixture(autouse=True)
def _reset_state():
    main._rate_hits.clear()
    main.state.engine, main.state.error = None, None
    main.state.ready.clear()
    yield


@pytest.fixture()
def client():
    return TestClient(main.app)          # no `with`: the model-loading lifespan is not started


def email():
    return f"{PREFIX}{uuid.uuid4().hex[:10]}@example.com"


def signup(client, **over):
    body = dict(first_name="Ada", last_name="Lovelace", email_address=email(), password=GOOD_PW, confirm_password=GOOD_PW)
    body.update(over)
    if "password" in over and "confirm_password" not in over:
        body["confirm_password"] = over["password"]
    return client.post("/signup", json=body), body


def auth(token):
    return {"Authorization": f"Bearer {token}"}


class FakeEngine:
    def __init__(self, events=None):
        self.events = events or [
            {"type": "status", "content": "Searching"}, {"type": "sources", "sources": [], "meta": {}},
            {"type": "thinking_done"}, {"type": "answer", "content": "21 days"}, {"type": "done"},
        ]
        self.seen = []

    def stream_answer(self, q, history):
        self.seen.append((q, history))
        yield from self.events

    class store:
        @staticmethod
        def get_document(doc_id):
            return None


def ready(engine):
    main.state.engine = engine
    main.state.ready.set()


def sse(resp):
    return [json.loads(l[6:]) for l in resp.text.splitlines() if l.startswith("data: ")]


@pytest.fixture()
def token(client):
    r, _ = signup(client)
    assert r.status_code == 201
    return r.json()["access_token"]


# ── signup / login ────────────────────────────────────────────────────────────
class TestSignup:
    def test_success_returns_token_and_name(self, client):
        r, _ = signup(client)
        assert r.status_code == 201 and r.json()["user_name"] == "Ada Lovelace" and r.json()["access_token"]

    def test_token_carries_name_claims_for_lms_provisioning(self, client):
        claims = jwt.decode(signup(client)[0].json()["access_token"], JWT_SECRET, algorithms=[ALGORITHM])
        assert claims["first_name"] == "Ada" and claims["last_name"] == "Lovelace" and "exp" in claims and "iat" in claims

    def test_duplicate_email_is_case_insensitive(self, client):
        r, body = signup(client)
        again = client.post("/signup", json={**body, "email_address": body["email_address"].upper()})
        assert again.status_code == 400 and "already" in again.json()["detail"]

    def test_email_is_stored_lowercase(self, client):
        r, body = signup(client, email_address=f"{PREFIX}MiXeD{uuid.uuid4().hex[:6]}@Example.COM")
        assert r.status_code == 201
        db = SessionLocal()
        assert db.query(User).filter(User.email == body["email_address"].lower()).count() == 1
        db.close()

    @pytest.mark.parametrize("over,needle", [
        ({"password": "short1"}, "password"),
        ({"password": "x" * 73}, "72 bytes"),
        ({"password": "é" * 40}, "72 bytes"),                 # 80 bytes, only 40 characters
        ({"confirm_password": "different-pass"}, "do not match"),
        ({"email_address": "not-an-email"}, "email"),
        ({"email_address": "a@b"}, "email"),
        ({"first_name": ""}, "first_name"),
        ({"first_name": "   "}, "blank"),
        ({"last_name": "x" * 51}, "last_name"),
    ])
    def test_invalid_input_is_a_readable_422(self, client, over, needle):
        r, _ = signup(client, **over)
        assert r.status_code == 422 and isinstance(r.json()["detail"], str) and needle in r.json()["detail"]

    def test_overlong_email_rejected_not_500(self, client):
        long = f"{PREFIX}{'a' * 110}@example.com"
        r, _ = signup(client, email_address=long)
        assert r.status_code == 422

    def test_unicode_password_and_names_work(self, client):
        r, body = signup(client, first_name="Zoë", last_name="Müller", password="pässwörd-日本")
        assert r.status_code == 201
        login = client.post("/login", json={"email_address": body["email_address"], "password": "pässwörd-日本"})
        assert login.status_code == 200 and login.json()["user_name"] == "Zoë Müller"

    def test_sql_injection_in_fields_is_inert(self, client):
        r, body = signup(client, first_name="Robert'); DROP TABLE users;--")
        assert r.status_code == 201
        assert client.post("/login", json={"email_address": body["email_address"], "password": GOOD_PW}).status_code == 200

    def test_malformed_json_and_wrong_types(self, client):
        assert client.post("/signup", content=b"{oops", headers={"Content-Type": "application/json"}).status_code == 422
        assert client.post("/signup", json=["a"]).status_code == 422
        assert client.post("/signup", json={"first_name": 5}).status_code == 422


class TestLogin:
    def test_success_and_case_insensitive_email(self, client):
        _, body = signup(client)
        r = client.post("/login", json={"email_address": body["email_address"].upper(), "password": GOOD_PW})
        assert r.status_code == 200 and r.json()["access_token"]

    def test_wrong_password_and_unknown_user_are_indistinguishable(self, client):
        _, body = signup(client)
        wrong = client.post("/login", json={"email_address": body["email_address"], "password": "nope-nope-nope"})
        ghost = client.post("/login", json={"email_address": email(), "password": "nope-nope-nope"})
        assert wrong.status_code == ghost.status_code == 401 and wrong.json() == ghost.json()

    def test_overlong_password_is_401_not_500(self, client):
        _, body = signup(client)
        assert client.post("/login", json={"email_address": body["email_address"], "password": "x" * 5000}).status_code == 422
        assert client.post("/login", json={"email_address": body["email_address"], "password": "x" * 200}).status_code == 401

    def test_bruteforce_is_throttled(self, client):
        codes = [client.post("/login", json={"email_address": email(), "password": "wrong-wrong"}).status_code for _ in range(12)]
        assert codes[:10] == [401] * 10 and codes[10] == 429
        retry = client.post("/login", json={"email_address": email(), "password": "wrong-wrong"})
        assert int(retry.headers["Retry-After"]) >= 1

    def test_no_enumeration_timing_gap_is_bounded(self, client):
        _, body = signup(client)
        def t(addr):
            main._rate_hits.clear()
            s = time.perf_counter()
            client.post("/login", json={"email_address": addr, "password": "wrong-wrong"})
            return time.perf_counter() - s
        real, ghost = t(body["email_address"]), t(email())
        assert ghost > real * 0.4      # a missing account still pays for a bcrypt verify


# ── authentication of the RAG endpoint ────────────────────────────────────────
class TestAskAuth:
    def test_no_token_is_401_not_403(self, client):
        r = client.post("/api/ask", json={"query": "hi"})
        assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"

    def test_garbage_token(self, client):
        assert client.post("/api/ask", json={"query": "hi"}, headers=auth("garbage")).status_code == 401

    def test_wrong_scheme(self, client):
        assert client.post("/api/ask", json={"query": "hi"}, headers={"Authorization": "Basic abc"}).status_code == 401

    def test_token_signed_with_other_secret(self, client):
        forged = jwt.encode({"sub": "x@example.com", "exp": datetime.now(timezone.utc) + timedelta(hours=1)}, "attacker", algorithm=ALGORITHM)
        assert client.post("/api/ask", json={"query": "hi"}, headers=auth(forged)).status_code == 401

    def test_expired_token(self, client):
        _, body = signup(client)
        old = jwt.encode({"sub": body["email_address"], "exp": datetime.now(timezone.utc) - timedelta(seconds=5)}, JWT_SECRET, algorithm=ALGORITHM)
        assert client.post("/api/ask", json={"query": "hi"}, headers=auth(old)).status_code == 401

    def test_alg_none_token_is_refused(self, client):
        _, body = signup(client)
        import base64
        b = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
        none_token = f"{b({'alg': 'none', 'typ': 'JWT'})}.{b({'sub': body['email_address']})}."
        assert client.post("/api/ask", json={"query": "hi"}, headers=auth(none_token)).status_code == 401

    def test_valid_token_for_deleted_user(self, client):
        r, body = signup(client)
        db = SessionLocal(); db.query(User).filter(User.email == body["email_address"]).delete(); db.commit(); db.close()
        assert client.post("/api/ask", json={"query": "hi"}, headers=auth(r.json()["access_token"])).status_code == 401

    def test_page_endpoint_requires_auth(self, client):
        assert client.get("/api/pages/doc/1").status_code == 401


# ── streaming behaviour ───────────────────────────────────────────────────────
class TestAskStreaming:
    def test_streams_sse_events(self, client, token):
        ready(FakeEngine())
        r = client.post("/api/ask", json={"query": "annual leave?"}, headers=auth(token))
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        assert r.headers["x-accel-buffering"] == "no"
        assert [e["type"] for e in sse(r)] == ["status", "sources", "thinking_done", "answer", "done"]

    def test_history_is_forwarded_as_tuples(self, client, token):
        eng = FakeEngine(); ready(eng)
        client.post("/api/ask", json={"query": "and part-time?", "history": [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]}, headers=auth(token))
        assert eng.seen[0] == ("and part-time?", [("user", "a"), ("assistant", "b")])

    @pytest.mark.parametrize("payload", [
        {}, {"query": 5}, {"query": "x" * 5001},
        {"query": "q", "history": [{"role": "system", "content": "evil"}]},
        {"query": "q", "history": [{"role": "user", "content": "x" * 4001}]},
        {"query": "q", "history": [{"role": "user", "content": "a"}] * 13},
    ])
    def test_bad_payloads_are_422(self, client, token, payload):
        ready(FakeEngine())
        r = client.post("/api/ask", json=payload, headers=auth(token))
        assert r.status_code == 422 and isinstance(r.json()["detail"], str)

    def test_engine_error_event_passes_through(self, client, token):
        ready(FakeEngine([{"type": "error", "code": "empty_query", "content": "Please type a question."}]))
        assert sse(client.post("/api/ask", json={"query": " "}, headers=auth(token)))[-1]["code"] == "empty_query"

    def test_engine_boot_failure_is_reported_cleanly(self, client, token):
        main.state.error = "The knowledge base is empty."
        main.state.ready.set()
        ev = sse(client.post("/api/ask", json={"query": "hi"}, headers=auth(token)))
        assert ev[-1] == {"type": "error", "content": "The knowledge base is empty."}

    def test_unexpected_exception_does_not_leak_internals(self, client, token):
        class Boom(FakeEngine):
            def stream_answer(self, q, h):
                raise ZeroDivisionError("secret internals")
                yield
        ready(Boom())
        text = client.post("/api/ask", json={"query": "hi"}, headers=auth(token)).text
        assert "secret internals" not in text and "Something went wrong" in text

    def test_per_user_rate_limit_and_isolation(self, client, token):
        ready(FakeEngine())
        other = signup(client)[0].json()["access_token"]
        codes = [client.post("/api/ask", json={"query": "hi"}, headers=auth(token)).status_code for _ in range(22)]
        assert codes[:20] == [200] * 20 and codes[20:] == [429, 429]
        assert client.post("/api/ask", json={"query": "hi"}, headers=auth(other)).status_code == 200
        r = client.post("/api/ask", json={"query": "hi"}, headers=auth(token))
        assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1

    def test_unicode_survives_the_wire(self, client, token):
        ready(FakeEngine([{"type": "answer", "content": "日本語 — “quotes” ✓"}, {"type": "done"}]))
        assert sse(client.post("/api/ask", json={"query": "q"}, headers=auth(token)))[0]["content"] == "日本語 — “quotes” ✓"


# ── misc endpoints / headers ──────────────────────────────────────────────────
class TestMisc:
    def test_healthz_is_independent_of_model_loading(self, client):
        assert client.get("/healthz").json() == {"ok": True}

    def test_status_reports_loading_ready_and_error(self, client):
        assert client.get("/api/status").json()["ready"] is False
        main.state.ready.set()
        assert client.get("/api/status").json()["ready"] is True
        main.state.error = "boom"
        body = client.get("/api/status").json()
        assert body["ready"] is False and body["error"] == "boom"

    def test_security_headers_and_request_id(self, client):
        r = client.get("/healthz", headers={"X-Request-ID": "abc123"})
        assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-frame-options"] == "DENY"
        assert r.headers["x-request-id"] == "abc123"

    def test_root_redirects_to_login(self, client):
        r = client.get("/", follow_redirects=False)
        assert r.status_code in (302, 307) and r.headers["location"] == "/frontend/login.html"

    def test_cors_blocks_unknown_origin_but_allows_ours(self, client):
        evil = client.options("/api/ask", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
        assert "access-control-allow-origin" not in evil.headers
        good = client.options("/api/ask", headers={"Origin": "http://localhost:8000", "Access-Control-Request-Method": "POST"})
        assert good.headers["access-control-allow-origin"] == "http://localhost:8000"

    def test_static_frontend_is_served(self, client):
        assert client.get("/frontend/login.html").status_code == 200
        assert client.get("/frontend/vendor/purify.min.js").status_code == 200

    def test_static_path_traversal_blocked(self, client):
        for path in ("/frontend/../.env", "/frontend/%2e%2e/.env", "/frontend/..%2f.env"):
            r = client.get(path)
            assert r.status_code in (400, 404) and "GROQ" not in r.text

    def test_page_image_unknown_doc_404(self, client, token):
        ready(FakeEngine())
        assert client.get("/api/pages/nope/1", headers=auth(token)).status_code == 404

    def test_page_image_rejects_non_integer_page(self, client, token):
        ready(FakeEngine())
        assert client.get("/api/pages/doc/abc", headers=auth(token)).status_code == 422
