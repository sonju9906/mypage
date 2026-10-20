"""
auth_api.py — 회원가입 · 로그인 · 로그아웃 (서버 계정)

붙이는 법 (다른 라우터와 같다)
    from auth_api import router as auth_router
    app.include_router(auth_router)

화면
    static/log.html      로그인
    static/signin.html   회원가입
    static/auth.js       모든 화면이 공통으로 쓰는 로그인 상태 표시 · 로그아웃

API  (에러 형식은 a.html 과 같다: {"error": {"code": "..."}})
    POST   /auth/signup    JSON {email, password, name, agree}   → 가입 후 바로 로그인
    POST   /auth/login     JSON {email, password}
    POST   /auth/logout
    GET    /auth/me        → {"logged_in": bool, "name", "email", "created_at"}
    POST   /auth/password  JSON {current, new}                   비밀번호 변경
    DELETE /auth/account   JSON {password}                       회원 탈퇴 (기록·사진도 삭제)

사용자 식별 — 이 파일이 생기면서 바뀐 점
    gemini_api.current_user_id() 가
        로그인했으면  "acct:<회원번호>"
        아니면        기존처럼 브라우저 쿠키 hm_uid
    를 돌려준다. 마이페이지·생성 횟수가 모두 이 값을 쓰므로 따로 고칠 곳이 없다.
    로그인하는 순간 그 브라우저의 게스트 기록(분석 기록·저장 사진)은 계정으로 옮겨진다.

보안
    - 비밀번호는 PBKDF2-SHA256(31만 회) + 사용자별 salt 로만 저장한다. 원문은 어디에도 남지 않는다.
    - 세션 토큰은 DB 에 SHA-256 해시로만 둔다. DB 가 새도 토큰을 쓸 수 없다.
    - 쿠키 hm_session: HttpOnly(자바스크립트가 못 읽음), SameSite=Lax.
      ★ https 로 배포하면 COOKIE_SECURE=1 환경변수를 켤 것.
    - 같은 이메일로 10분에 5번 틀리면 잠시 막는다.
    - 로그인 실패 메시지는 '이메일이 없음/비번 틀림'을 구분하지 않는다(가입 여부 노출 방지).
"""

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
DB_PATH = HERE / "mypage.db"               # 마이페이지와 같은 파일. 학교 PC 에서 파일 하나만 챙기면 된다
SESSION_COOKIE = "hm_session"
SESSION_DAYS = 14
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "0") == "1"
PBKDF2_ITER = 310_000
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[A-Za-z]{2,}$")
MAX_FAILS, FAIL_WINDOW = 5, 600            # 10분에 5번

_fails: dict = {}                          # email → [실패 시각, ...]  (서버 재시작 시 초기화)


# ═════════════════════════ 저장소 — FastAPI 와 무관한 순수 함수 ═════════════════════════
def _db(path: Optional[Path] = None):
    con = sqlite3.connect(path or DB_PATH)
    con.row_factory = sqlite3.Row
    con.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            email      TEXT NOT NULL UNIQUE,
            name       TEXT NOT NULL,
            pw_hash    TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY,
            user_id    INTEGER NOT NULL,
            expires_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_sessions_user ON sessions(user_id);
    """)
    return con


def _now() -> datetime:
    return datetime.now(timezone.utc)


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, PBKDF2_ITER)
    return f"pbkdf2_sha256${PBKDF2_ITER}${salt.hex()}${dk.hex()}"


def check_password(pw: str, stored: str) -> bool:
    try:
        _, it, salt, h = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), int(it))
        return hmac.compare_digest(dk.hex(), h)
    except (ValueError, TypeError):
        return False


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def norm_email(email: str) -> str:
    return (email or "").strip().lower()


def validate_signup(email: str, password: str, name: str) -> Optional[str]:
    """문제가 있으면 에러 코드, 없으면 None"""
    if not EMAIL_RE.match(email) or len(email) > 254:
        return "INVALID_EMAIL"
    if not (1 <= len(name.strip()) <= 20):
        return "INVALID_NAME"
    return validate_password(password)


def validate_password(pw: str) -> Optional[str]:
    if not (8 <= len(pw or "") <= 128) or not re.search(r"[A-Za-z]", pw) or not re.search(r"\d", pw):
        return "WEAK_PASSWORD"            # 8자 이상, 영문 + 숫자
    return None


def create_user(con, email: str, password: str, name: str):
    """(user_id, None) 또는 (None, 에러코드)"""
    email = norm_email(email)
    err = validate_signup(email, password, name)
    if err:
        return None, err
    try:
        cur = con.execute(
            "INSERT INTO users(email, name, pw_hash, created_at) VALUES(?,?,?,?)",
            (email, name.strip(), hash_password(password), _now().isoformat(timespec="seconds")))
    except sqlite3.IntegrityError:
        return None, "EMAIL_TAKEN"
    return cur.lastrowid, None


def authenticate(con, email: str, password: str) -> Optional[int]:
    row = con.execute("SELECT id, pw_hash FROM users WHERE email=?", (norm_email(email),)).fetchone()
    if row is None:
        check_password(password, hash_password("dummy-for-timing1"))   # 없는 이메일도 같은 시간이 걸리게
        return None
    return row["id"] if check_password(password, row["pw_hash"]) else None


def new_session(con, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    exp = (_now() + timedelta(days=SESSION_DAYS)).isoformat(timespec="seconds")
    con.execute("INSERT INTO sessions VALUES(?,?,?)", (_token_hash(token), user_id, exp))
    con.execute("DELETE FROM sessions WHERE expires_at < ?", (_now().isoformat(timespec="seconds"),))
    return token


def session_user(con, token: Optional[str]) -> Optional[sqlite3.Row]:
    if not token:
        return None
    return con.execute(
        "SELECT u.id, u.email, u.name, u.created_at FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.token_hash=? AND s.expires_at > ?",
        (_token_hash(token), _now().isoformat(timespec="seconds"))).fetchone()


def end_session(con, token: Optional[str]) -> None:
    if token:
        con.execute("DELETE FROM sessions WHERE token_hash=?", (_token_hash(token),))


def change_password(con, user_id: int, current: str, new: str) -> Optional[str]:
    row = con.execute("SELECT pw_hash FROM users WHERE id=?", (user_id,)).fetchone()
    if not row or not check_password(current, row["pw_hash"]):
        return "WRONG_PASSWORD"
    err = validate_password(new)
    if err:
        return err
    con.execute("UPDATE users SET pw_hash=? WHERE id=?", (hash_password(new), user_id))
    return None


def delete_user(con, user_id: int) -> None:
    con.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
    con.execute("DELETE FROM users WHERE id=?", (user_id,))


def acct_uid(user_id: int) -> str:
    """마이페이지·생성 횟수가 쓰는 사용자 키. 게스트 쿠키 값과 겹치지 않게 접두사를 붙인다."""
    return f"acct:{user_id}"


def too_many_fails(email: str) -> bool:
    now = time.time()
    lst = [t for t in _fails.get(email, []) if now - t < FAIL_WINDOW]
    _fails[email] = lst
    return len(lst) >= MAX_FAILS


def note_fail(email: str) -> None:
    _fails.setdefault(email, []).append(time.time())


# ═════════════════════════ 라우트 ═════════════════════════
try:
    from fastapi import APIRouter, Request
    from fastapi.responses import JSONResponse
    from pydantic import BaseModel, Field
except ImportError:          # FastAPI 없이 위 함수만 테스트할 때
    APIRouter = None


def account_uid(request) -> Optional[str]:
    """로그인했으면 'acct:N', 아니면 None. gemini_api.current_user_id() 가 부른다."""
    with _db() as con:
        u = session_user(con, request.cookies.get(SESSION_COOKIE))
    return acct_uid(u["id"]) if u else None


if APIRouter is not None:
    router = APIRouter()

    class SignupIn(BaseModel):
        email: str = Field(..., max_length=254)
        password: str = Field(..., max_length=128)
        name: str = Field(..., max_length=20)
        agree: bool = False

    class LoginIn(BaseModel):
        email: str = Field(..., max_length=254)
        password: str = Field(..., max_length=128)

    class PasswordIn(BaseModel):
        current: str = Field(..., max_length=128)
        new: str = Field(..., max_length=128)

    class DeleteIn(BaseModel):
        password: str = Field(..., max_length=128)

    def _err(status: int, code: str) -> JSONResponse:
        return JSONResponse(status_code=status, content={"error": {"code": code}})

    def _me_json(u) -> dict:
        return {"logged_in": True, "name": u["name"], "email": u["email"], "created_at": u["created_at"]}

    def _login_response(con, request: Request, user_id: int, status: int = 200) -> JSONResponse:
        # ★ 먼저 커밋한다. 가입 INSERT 가 열린 채로 아래에서 다른 연결이 같은 DB 에 쓰면 'database is locked'
        con.commit()
        # 이 브라우저의 게스트 기록을 계정으로 옮긴다
        from gemini_api import COOKIE as GUEST_COOKIE
        from mypage_api import merge_guest_into
        guest = request.cookies.get(GUEST_COOKIE)
        if guest:
            try:
                merge_guest_into(guest, acct_uid(user_id))
            except Exception as e:                 # 합치기가 실패해도 로그인은 된다
                print(f"[auth] 게스트 기록 합치기 실패: {type(e).__name__}")
        token = new_session(con, user_id)
        u = con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        resp = JSONResponse(status_code=status, content=_me_json(u), headers={"Cache-Control": "no-store"})
        resp.set_cookie(SESSION_COOKIE, token, max_age=SESSION_DAYS * 86400,
                        httponly=True, samesite="lax", secure=COOKIE_SECURE)
        return resp

    @router.post("/auth/signup")
    async def signup(body: SignupIn, request: Request):
        if not body.agree:
            return _err(400, "AGREE_REQUIRED")
        with _db() as con:
            uid, err = create_user(con, body.email, body.password, body.name)
            if err:
                return _err(409 if err == "EMAIL_TAKEN" else 400, err)
            return _login_response(con, request, uid, 201)

    @router.post("/auth/login")
    async def login(body: LoginIn, request: Request):
        email = norm_email(body.email)
        if too_many_fails(email):
            return _err(429, "TOO_MANY_ATTEMPTS")
        with _db() as con:
            uid = authenticate(con, email, body.password)
            if uid is None:
                note_fail(email)
                return _err(401, "BAD_CREDENTIALS")
            _fails.pop(email, None)
            return _login_response(con, request, uid)

    @router.post("/auth/logout")
    async def logout(request: Request):
        with _db() as con:
            end_session(con, request.cookies.get(SESSION_COOKIE))
        resp = JSONResponse(content={"logged_in": False})
        resp.delete_cookie(SESSION_COOKIE)
        return resp

    @router.get("/auth/me")
    async def me(request: Request):
        with _db() as con:
            u = session_user(con, request.cookies.get(SESSION_COOKIE))
        return JSONResponse(content=_me_json(u) if u else {"logged_in": False},
                            headers={"Cache-Control": "no-store"})

    @router.post("/auth/password")
    async def password(body: PasswordIn, request: Request):
        with _db() as con:
            u = session_user(con, request.cookies.get(SESSION_COOKIE))
            if not u:
                return _err(401, "NOT_LOGGED_IN")
            err = change_password(con, u["id"], body.current, body.new)
            if err:
                return _err(400, err)
            # 다른 기기에 남은 로그인은 끊고 지금 기기만 유지
            keep = _token_hash(request.cookies.get(SESSION_COOKIE))
            con.execute("DELETE FROM sessions WHERE user_id=? AND token_hash<>?", (u["id"], keep))
        return {"changed": True}

    @router.delete("/auth/account")
    async def delete_account(body: DeleteIn, request: Request):
        with _db() as con:
            u = session_user(con, request.cookies.get(SESSION_COOKIE))
            if not u:
                return _err(401, "NOT_LOGGED_IN")
            if authenticate(con, u["email"], body.password) is None:
                return _err(400, "WRONG_PASSWORD")
            con.commit()                           # 다른 연결이 쓰기 전에 (위와 같은 이유)
            from mypage_api import wipe_uid
            wipe_uid(acct_uid(u["id"]))
            delete_user(con, u["id"])
        resp = JSONResponse(content={"deleted": True})
        resp.delete_cookie(SESSION_COOKIE)
        return resp
