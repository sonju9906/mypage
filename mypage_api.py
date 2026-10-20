"""
mypage_api.py — 마이페이지 백엔드

붙이는 법 (salons_api.py, gemini_api.py 와 같다)
    from mypage_api import router as mypage_router, save_analysis
    app.include_router(mypage_router)
    → 함께 보낸 web_main.py / map_test_server.py / gemini_api.py 에 이미 들어가 있다

화면
    static/mypage.html            http://localhost:8000/mypage.html
    계정 드롭다운의 네 메뉴가 각각 #account / #history / #saved / #security 로 온다.

API  (에러 형식은 a.html 과 같다: {"error": {"code": "..."}})
    GET    /me                       계정 정보 요약 (로그인 정보, 생성 남은 횟수, 개수, 설정)
    GET    /me/history               얼굴형 분석 기록 (최근순, 최대 20건)
    DELETE /me/history/{id}
    GET    /me/photos?kind=          저장한 사진. kind = synth(추천 합성) | prompt(프롬프트 생성) | 생략=전부
    POST   /me/photos                합성 결과 사진 저장 (multipart: file, slug, title, face_shape?, gender?)
    GET    /me/photos/{id}/image     사진 — 본인 것일 때만 내려간다
    DELETE /me/photos/{id}
    PATCH  /me/settings              JSON {save_history: bool}
    DELETE /me                       내 기록·사진 전부 삭제 (생성 횟수는 초기화하지 않음)

무엇을 저장하나
    - 업로드한 원본 얼굴 사진: 저장하지 않는다 (web_main.py 원칙 그대로)
    - 얼굴형 분석: 얼굴형 이름·확률·성별·시각만
    - 사진: '합성 결과'만. a.html 의 [마이페이지에 저장] 을 누른 것(synth)과
            화면 4 에서 프롬프트로 만든 것(prompt, 자동 저장).
      다시 인코딩해 메타데이터(EXIF)를 지우고 긴 변 1024px JPEG 로 gen_store/ 에 둔다.
      static/ 밖이라 주소를 알아도 브라우저로 직접 열 수 없고, API 는 주인이 아니면 404.

사용자 식별
    gemini_api.current_user_id() — 로그인했으면 'acct:<번호>', 아니면 브라우저 쿠키 hm_uid.
    로그인하는 순간 auth_api 가 merge_guest_into() 로 게스트 기록을 계정으로 옮긴다.
"""

import io
import json
import re
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
DB_PATH = HERE / "mypage.db"
GEN_DIR = HERE / "gen_store"            # ★ static/ 밖. 브라우저가 직접 열 수 없어야 한다
COOKIE = "hm_uid"
COOKIE_AGE = 60 * 60 * 24 * 30
MAX_HISTORY = 20
MAX_PHOTOS = 30                         # 사용자가 누른 저장(synth) 기준 한도
MAX_UPLOAD = 8 * 1024 * 1024
MAX_SIDE = 1024
SLUG_RE = re.compile(r"^[a-z0-9_]{1,40}$")
FACE_SHAPES = {"Heart", "Oblong", "Oval", "Round", "Square"}
GENDERS = {"male", "female"}
KINDS = {"synth", "prompt"}


# ═════════════════════════ 저장소 — FastAPI 와 무관한 순수 함수 ═════════════════════════
def _db(path: Optional[Path] = None):
    con = sqlite3.connect(path or DB_PATH)
    con.row_factory = sqlite3.Row
    con.executescript("""
        CREATE TABLE IF NOT EXISTS mp_user (
            uid          TEXT PRIMARY KEY,
            first_seen   TEXT NOT NULL,
            save_history INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS mp_history (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            uid        TEXT NOT NULL,
            created_at TEXT NOT NULL,
            gender     TEXT NOT NULL,
            face_shape TEXT NOT NULL,
            name_ko    TEXT,
            confidence REAL,
            ambiguous  INTEGER NOT NULL DEFAULT 0,
            dist_json  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_mp_history_uid ON mp_history(uid, id);
        CREATE TABLE IF NOT EXISTS mp_photo (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            uid        TEXT NOT NULL,
            created_at TEXT NOT NULL,
            kind       TEXT NOT NULL,          -- synth | prompt
            title      TEXT NOT NULL,          -- 스타일 이름 또는 프롬프트
            slug       TEXT,
            face_shape TEXT,
            gender     TEXT,
            file       TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_mp_photo_uid ON mp_photo(uid, id);
    """)
    return con


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_user(con, uid: str) -> sqlite3.Row:
    con.execute("INSERT OR IGNORE INTO mp_user(uid, first_seen) VALUES(?, ?)", (uid, _now()))
    return con.execute("SELECT * FROM mp_user WHERE uid=?", (uid,)).fetchone()


# ── 분석 기록 ──
def record_analysis(con, uid: str, result: dict) -> Optional[int]:
    """/analyze 응답 dict 를 기록. 기록을 꺼 두었거나 형식이 이상하면 None."""
    user = ensure_user(con, uid)
    if not user["save_history"]:
        return None
    primary = result.get("primary") or {}
    code = primary.get("code") or result.get("face_shape")
    if code not in FACE_SHAPES:
        return None
    dist = [{"code": d.get("code"), "name_ko": d.get("name_ko"),
             "probability": float(d.get("probability") or 0)}
            for d in (result.get("distribution") or [])][:5]
    cur = con.execute(
        "INSERT INTO mp_history(uid, created_at, gender, face_shape, name_ko, confidence, ambiguous, dist_json)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (uid, _now(), result.get("gender", ""), code, primary.get("name_ko"),
         primary.get("probability", result.get("confidence")),
         1 if result.get("is_ambiguous", result.get("ambiguous")) else 0,
         json.dumps(dist, ensure_ascii=False)))
    _trim_history(con, uid)
    return cur.lastrowid


def _trim_history(con, uid: str) -> None:
    con.execute(
        "DELETE FROM mp_history WHERE uid=? AND id NOT IN "
        "(SELECT id FROM mp_history WHERE uid=? ORDER BY id DESC LIMIT ?)",
        (uid, uid, MAX_HISTORY))


def list_history(con, uid: str) -> list:
    rows = con.execute("SELECT * FROM mp_history WHERE uid=? ORDER BY id DESC", (uid,)).fetchall()
    return [{"id": r["id"], "created_at": r["created_at"], "gender": r["gender"],
             "face_shape": r["face_shape"], "name_ko": r["name_ko"],
             "confidence": r["confidence"], "ambiguous": bool(r["ambiguous"]),
             "distribution": json.loads(r["dist_json"])} for r in rows]


def delete_history(con, uid: str, hid: int) -> bool:
    return con.execute("DELETE FROM mp_history WHERE uid=? AND id=?", (uid, hid)).rowcount > 0


# ── 사진 ──
def clean_image(raw: bytes) -> bytes:
    """이미지인지 확인하고, EXIF 등 메타데이터를 버린 긴 변 1024px JPEG 로 다시 만든다.
    이미지가 아니면 ValueError."""
    from PIL import Image, UnidentifiedImageError
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except (UnidentifiedImageError, OSError) as e:
        raise ValueError("BAD_IMAGE") from e
    if img.format not in ("PNG", "JPEG", "WEBP"):
        raise ValueError("BAD_IMAGE")
    img = img.convert("RGB")
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=92)
    return out.getvalue()


def record_photo(con, uid: str, kind: str, title: str, raw: bytes,
                 slug: Optional[str] = None, face_shape: Optional[str] = None,
                 gender: Optional[str] = None, gen_dir: Optional[Path] = None):
    """(photo_id, None) 또는 (None, 에러코드: INVALID / BAD_IMAGE / PHOTOS_FULL)"""
    title = " ".join((title or "").split())[:200]
    if kind not in KINDS or not title:
        return None, "INVALID"
    if slug is not None and not SLUG_RE.match(slug):
        return None, "INVALID"
    if face_shape not in (None, *FACE_SHAPES) or gender not in (None, *GENDERS):
        return None, "INVALID"
    if len(raw) > MAX_UPLOAD:
        return None, "BAD_IMAGE"
    if kind == "synth":
        n = con.execute("SELECT COUNT(*) FROM mp_photo WHERE uid=? AND kind='synth'", (uid,)).fetchone()[0]
        if n >= MAX_PHOTOS:
            return None, "PHOTOS_FULL"
    try:
        jpeg = clean_image(raw)
    except ValueError:
        return None, "BAD_IMAGE"
    gen_dir = gen_dir or GEN_DIR
    gen_dir.mkdir(exist_ok=True)
    ensure_user(con, uid)
    name = secrets.token_hex(12) + ".jpg"        # 추측할 수 없는 이름. uid 를 넣지 않는다
    (gen_dir / name).write_bytes(jpeg)
    cur = con.execute(
        "INSERT INTO mp_photo(uid, created_at, kind, title, slug, face_shape, gender, file)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (uid, _now(), kind, title, slug, face_shape, gender, name))
    return cur.lastrowid, None


def list_photos(con, uid: str, kind: Optional[str] = None) -> list:
    q, args = "SELECT * FROM mp_photo WHERE uid=?", [uid]
    if kind in KINDS:
        q += " AND kind=?"; args.append(kind)
    rows = con.execute(q + " ORDER BY id DESC", args).fetchall()
    return [{"id": r["id"], "created_at": r["created_at"], "kind": r["kind"], "title": r["title"],
             "slug": r["slug"], "face_shape": r["face_shape"], "gender": r["gender"],
             "image_url": f"/me/photos/{r['id']}/image"} for r in rows]


def get_photo(con, uid: str, pid: int, gen_dir: Optional[Path] = None) -> Optional[bytes]:
    r = con.execute("SELECT file FROM mp_photo WHERE uid=? AND id=?", (uid, pid)).fetchone()
    if not r:
        return None
    p = (gen_dir or GEN_DIR) / r["file"]
    return p.read_bytes() if p.is_file() else None


def delete_photo(con, uid: str, pid: int, gen_dir: Optional[Path] = None) -> bool:
    r = con.execute("SELECT file FROM mp_photo WHERE uid=? AND id=?", (uid, pid)).fetchone()
    if not r:
        return False
    con.execute("DELETE FROM mp_photo WHERE id=?", (pid,))
    (gen_dir or GEN_DIR).joinpath(r["file"]).unlink(missing_ok=True)
    return True


# ── 설정 · 전체 삭제 · 합치기 · 요약 ──
def set_save_history(con, uid: str, on: bool) -> None:
    ensure_user(con, uid)
    con.execute("UPDATE mp_user SET save_history=? WHERE uid=?", (1 if on else 0, uid))


def wipe_user(con, uid: str, gen_dir: Optional[Path] = None) -> None:
    """기록·사진·설정 삭제. 생성 횟수(quota.db)는 건드리지 않는다
    — 지울 때마다 무료 2회가 다시 생기면 안 되므로."""
    for r in con.execute("SELECT file FROM mp_photo WHERE uid=?", (uid,)).fetchall():
        (gen_dir or GEN_DIR).joinpath(r["file"]).unlink(missing_ok=True)
    con.execute("DELETE FROM mp_photo WHERE uid=?", (uid,))
    con.execute("DELETE FROM mp_history WHERE uid=?", (uid,))
    con.execute("DELETE FROM mp_user WHERE uid=?", (uid,))


def merge_user(con, guest: str, acct: str) -> None:
    """게스트 기록·사진을 계정으로 옮긴다. 처음 방문일은 더 이른 쪽을 남긴다."""
    if not guest or guest == acct or guest.startswith("acct:"):
        return
    g = con.execute("SELECT * FROM mp_user WHERE uid=?", (guest,)).fetchone()
    a = ensure_user(con, acct)
    if g and g["first_seen"] < a["first_seen"]:
        con.execute("UPDATE mp_user SET first_seen=? WHERE uid=?", (g["first_seen"], acct))
    con.execute("UPDATE mp_history SET uid=? WHERE uid=?", (acct, guest))
    con.execute("UPDATE mp_photo SET uid=? WHERE uid=?", (acct, guest))
    con.execute("DELETE FROM mp_user WHERE uid=?", (guest,))
    _trim_history(con, acct)


def summary(con, uid: str, used: int, limit: int, account: Optional[dict] = None) -> dict:
    user = ensure_user(con, uid)
    count = lambda sql: con.execute(sql, (uid,)).fetchone()[0]
    return {
        "account": account,                    # 로그인했으면 {name, email, created_at}, 아니면 None
        "guest_tag": None if account else uid[:6].upper(),
        "first_seen": user["first_seen"],
        "quota": {"limit": limit, "used": min(used, limit), "remaining": max(0, limit - used)},
        "history_count": count("SELECT COUNT(*) FROM mp_history WHERE uid=?"),
        "photo_count": count("SELECT COUNT(*) FROM mp_photo WHERE uid=?"),
        "settings": {"save_history": bool(user["save_history"])},
    }


# ── 다른 파일이 부르는 함수 (FastAPI 없이도 동작) ──
def merge_guest_into(guest: str, acct: str) -> None:
    """auth_api 가 로그인·가입 직후 부른다. 생성 횟수는 둘 중 많이 쓴 쪽으로 맞춘다
    (게스트로 써 놓고 가입해서 다시 2회를 받는 걸 막는다)."""
    with _db() as con:
        merge_user(con, guest, acct)
    from gemini_api import _add, _used
    g, a = _used(guest), _used(acct)
    if g > a:
        _add(acct, g - a)


def wipe_uid(uid: str) -> None:
    """auth_api 의 회원 탈퇴가 부른다."""
    with _db() as con:
        wipe_user(con, uid)


# ═════════════════════════ 라우트 ═════════════════════════
try:
    from fastapi import APIRouter, File, Form, Request, Response, UploadFile
    from fastapi.responses import JSONResponse
    from pydantic import BaseModel
except ImportError:          # FastAPI 없이 위 함수만 테스트할 때
    APIRouter = None

if APIRouter is not None:
    from gemini_api import FREE_LIMIT, _used, current_user_id

    router = APIRouter()

    class SettingsIn(BaseModel):
        save_history: bool

    def _err(status: int, code: str) -> JSONResponse:
        return JSONResponse(status_code=status, content={"error": {"code": code}})

    def _set_cookie(resp, uid: str) -> None:       # gemini_api.me_quota 와 같은 설정
        resp.set_cookie(COOKIE, uid, max_age=COOKIE_AGE, httponly=True, samesite="lax")

    def _uid(request: Request):
        """(uid, 새로 발급했는지). 마이페이지로 먼저 들어와도 쿠키가 생기게 한다."""
        uid = current_user_id(request)
        return (uid, False) if uid else (secrets.token_urlsafe(16), True)

    def _reply(content, uid: str, is_new: bool, status: int = 200) -> JSONResponse:
        resp = JSONResponse(content=content, status_code=status, headers={"Cache-Control": "no-store"})
        if is_new:
            _set_cookie(resp, uid)
        return resp

    def save_analysis(request: Request, response: Response, result: dict) -> None:
        """web_main.analyze() 끝에서 부른다. 쿠키가 없으면 여기서 발급(첫 방문자의 첫 분석도 기록).
        저장이 실패해도 분석 응답은 막지 않는다."""
        uid = current_user_id(request)
        if not uid:
            uid = secrets.token_urlsafe(16)
            _set_cookie(response, uid)
        try:
            with _db() as con:
                record_analysis(con, uid, result)
        except Exception as e:
            print(f"[mypage] 분석 기록 실패: {type(e).__name__}")

    def save_generated(uid: str, prompt: str, data: bytes, mime: str = "") -> None:
        """gemini_api.generate() 성공 직후 부른다(프롬프트 결과 자동 저장). 실패해도 응답은 막지 않는다."""
        try:
            with _db() as con:
                _, err = record_photo(con, uid, "prompt", prompt, data)
            if err:
                print(f"[mypage] 생성 결과 저장 안 됨: {err}")
        except Exception as e:
            print(f"[mypage] 생성 결과 저장 실패: {type(e).__name__}")

    # ── 계정 정보 ──
    @router.get("/me")
    async def me(request: Request):
        from auth_api import SESSION_COOKIE, _db as auth_db, session_user
        with auth_db() as con:
            u = session_user(con, request.cookies.get(SESSION_COOKIE))
        account = {"name": u["name"], "email": u["email"], "created_at": u["created_at"]} if u else None
        uid, is_new = _uid(request)
        with _db() as con:
            data = summary(con, uid, 0 if is_new else _used(uid), FREE_LIMIT, account)
        return _reply(data, uid, is_new)

    # ── 분석 기록 ──
    @router.get("/me/history")
    async def me_history(request: Request):
        uid, is_new = _uid(request)
        with _db() as con:
            return _reply(list_history(con, uid), uid, is_new)

    @router.delete("/me/history/{hid}")
    async def me_history_delete(hid: int, request: Request):
        uid = current_user_id(request)
        if not uid:
            return _err(401, "NOT_LOGGED_IN")
        with _db() as con:
            ok = delete_history(con, uid, hid)
        return {"deleted": hid} if ok else _err(404, "NOT_FOUND")

    # ── 사진 ──
    @router.get("/me/photos")
    async def me_photos(request: Request, kind: Optional[str] = None):
        uid, is_new = _uid(request)
        with _db() as con:
            return _reply(list_photos(con, uid, kind), uid, is_new)

    @router.post("/me/photos")
    async def me_photo_add(
        request: Request,
        file: UploadFile = File(...),
        title: str = Form(...),
        slug: Optional[str] = Form(None),
        face_shape: Optional[str] = Form(None),
        gender: Optional[str] = Form(None),
    ):
        uid, is_new = _uid(request)
        raw = await file.read(MAX_UPLOAD + 1)
        with _db() as con:
            pid, err = record_photo(con, uid, "synth", title, raw, slug or None,
                                    face_shape or None, gender or None)
        del raw
        if err:
            return _err({"PHOTOS_FULL": 409, "BAD_IMAGE": 400}.get(err, 400), err)
        return _reply({"id": pid, "image_url": f"/me/photos/{pid}/image"}, uid, is_new, 201)

    @router.get("/me/photos/{pid}/image")
    async def me_photo_image(pid: int, request: Request):
        uid = current_user_id(request)
        with _db() as con:
            data = get_photo(con, uid, pid) if uid else None
        if not data:
            return _err(404, "NOT_FOUND")
        return Response(content=data, media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=3600"})

    @router.delete("/me/photos/{pid}")
    async def me_photo_delete(pid: int, request: Request):
        uid = current_user_id(request)
        if not uid:
            return _err(401, "NOT_LOGGED_IN")
        with _db() as con:
            ok = delete_photo(con, uid, pid)
        return {"deleted": pid} if ok else _err(404, "NOT_FOUND")

    # ── 보안 설정 ──
    @router.patch("/me/settings")
    async def me_settings(body: SettingsIn, request: Request):
        uid, is_new = _uid(request)
        with _db() as con:
            set_save_history(con, uid, body.save_history)
        return _reply({"save_history": body.save_history}, uid, is_new)

    @router.delete("/me")
    async def me_wipe(request: Request):
        uid = current_user_id(request)
        if not uid:
            return _err(401, "NOT_LOGGED_IN")
        with _db() as con:
            wipe_user(con, uid)
        return {"wiped": True}
