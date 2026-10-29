"""Area do aluno (user.rangeltech.net): FastAPI + Jinja + SQLite. O backend decide tudo (acesso, janela de mentoria, midia)."""
import hashlib
import hmac
import json
import mimetypes
import os
import secrets
import shutil
from datetime import timedelta
from urllib.parse import quote

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import ciclo, core, db, extras, media

BASE = os.path.dirname(__file__)
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static"), html=False), name="static")
T = Jinja2Templates(directory=os.path.join(BASE, "templates"))
T.env.globals.update(fmt_local=core.fmt_local, fmt_dur=core.fmt_dur, mentoria_state=core.mentoria_state)
MAX_UPLOAD = 6 * 1024 ** 3
CONSENT_VERSION = "v1-2026-10"
ICE = json.loads(os.environ.get("ICE_SERVERS_JSON", '[{"urls":"stun:stun.l.google.com:19302"}]'))
KIWIFY_TOKEN = os.environ.get("KIWIFY_TOKEN", "")

STATE_LABEL = {"agendada": "Agendada", "entrada_liberada": "Entrada liberada", "em_andamento": "Em andamento",
               "concluida": "Concluída", "cancelada": "Cancelada"}
T.env.globals["STATE_LABEL"] = STATE_LABEL


class NeedLogin(Exception):
    pass


@app.on_event("startup")
def _startup():
    db.init()
    core.ensure_master()
    media.resume_pending()


@app.exception_handler(NeedLogin)
async def _need_login(request, exc):
    nxt = request.url.path if request.method == "GET" else "/"
    return RedirectResponse(f"/login?next={quote(nxt)}", 303)


@app.exception_handler(HTTPException)
async def _http(request, exc):
    if request.url.path.startswith(("/api/", "/media/")) or exc.status_code in (401,) and request.url.path.startswith("/ws"):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
    return T.TemplateResponse(request, "erro.html", {"user": None, "code": exc.status_code, "msg": exc.detail}, status_code=exc.status_code)


@app.middleware("http")
async def _headers(request, call_next):
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Permissions-Policy"] = "camera=(self), microphone=(self), display-capture=(self)"
    resp.headers["Content-Security-Policy"] = ("default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; "
                                               "connect-src 'self' wss: ws:; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                                               "worker-src 'self' blob:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
    if request.url.path.startswith(("/media/", "/api/", "/login", "/admin")) or request.method != "GET":
        resp.headers["Cache-Control"] = "no-store"
    return resp


# ---------------------------------------------------------------- utilidades
def me(request, admin=False):
    u = core.user_from_token(request.cookies.get("sid"))
    if not u:
        raise NeedLogin()
    if admin and u["role"] != "admin":
        raise HTTPException(403, "Área restrita ao administrador.")
    if u["must_change"] and request.url.path not in ("/perfil", "/perfil/senha", "/logout"):
        raise HTTPException(307, "must_change", headers={"Location": "/perfil?troca=1"})
    return u


@app.exception_handler(307)
async def _redir307(request, exc):
    return RedirectResponse(exc.headers["Location"], 303)


def page(request, name, user, **ctx):
    sup = False
    novas = []
    if user:
        extras.evaluate(user)
        sup = extras.has_support(user)
        novas = extras.unseen(user)
    return T.TemplateResponse(request, name, {"user": user, "csrf": user["csrf"] if user else "", "support": sup, "novas": novas,
                                              "ok": request.query_params.get("ok"), "erro": request.query_params.get("erro"), **ctx})


async def form(request, user):
    f = await request.form()
    if not core.csrf_ok(user, f.get("csrf")):
        raise HTTPException(403, "Sessão inválida. Recarregue a página.")
    return f


def back(path, ok=None, erro=None):
    sep = "&" if "?" in path else "?"
    if ok:
        path += f"{sep}ok={quote(ok)}"
    elif erro:
        path += f"{sep}erro={quote(erro)}"
    return RedirectResponse(path, 303)


def client_ip(request):
    return request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else "?")


def safe_next(n):
    return n if n and n.startswith("/") and not n.startswith("//") else "/"


async def save_upload(up, dest):
    size = 0
    with open(dest, "wb") as out:
        while True:
            chunk = await up.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_UPLOAD:
                out.close()
                os.remove(dest)
                raise HTTPException(413, "Arquivo grande demais.")
            out.write(chunk)
    return size


@app.get("/health")
def health():
    db.q("SELECT 1")
    return PlainTextResponse("ok")


# ---------------------------------------------------------------- login
@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if core.user_from_token(request.cookies.get("sid")):
        return RedirectResponse("/", 303)
    return T.TemplateResponse(request, "login.html", {"user": None, "erro": request.query_params.get("erro"), "next": safe_next(request.query_params.get("next"))})


@app.post("/login")
async def login_post(request: Request):
    f = await request.form()
    email = (f.get("email") or "").strip().lower()
    key = f"{client_ip(request)}|{email}"
    if core.login_blocked(key):
        return T.TemplateResponse(request, "login.html", {"user": None, "erro": "Muitas tentativas. Aguarde alguns minutos.", "next": "/"}, status_code=429)
    u = core.find_user(email)
    ok = core.check_pw(f.get("password") or "", u["pw_hash"]) if u else core.check_pw("x", "$2b$12$" + "a" * 53)
    if not (u and ok):
        core.login_fail(key)
        core.audit(None, "login_fail", email)
        return T.TemplateResponse(request, "login.html", {"user": None, "erro": "Login ou senha incorretos.", "next": safe_next(f.get("next"))}, status_code=401)
    core.login_ok(key)
    tok = core.new_session(u["id"])
    core.audit(u["id"], "login", email)
    r = RedirectResponse(safe_next(f.get("next")), 303)
    r.set_cookie("sid", tok, max_age=core.SESSION_DAYS * 86400, httponly=True, secure=request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https", samesite="lax", path="/")
    return r


@app.post("/logout")
async def logout(request: Request):
    u = me(request)
    await form(request, u)
    core.end_session(request.cookies.get("sid"))
    r = RedirectResponse("/login", 303)
    r.delete_cookie("sid")
    return r


# ---------------------------------------------------------------- aluno: inicio e cursos
def course_progress(user_id, course_id):
    rows = db.q("SELECT l.id, COALESCE(p.completed,0) c FROM lessons l JOIN modules m ON m.id=l.module_id "
                "LEFT JOIN progress p ON p.lesson_id=l.id AND p.user_id=? WHERE m.course_id=? AND l.video_status='ready'", (user_id, course_id))
    tot = len(rows)
    done = sum(r["c"] for r in rows)
    return tot, done, (round(100 * done / tot) if tot else 0)


def lessons_of(course_id):
    return db.q("SELECT l.*, m.title AS module_title, m.position AS mpos FROM lessons l JOIN modules m ON m.id=l.module_id "
                "WHERE m.course_id=? ORDER BY m.position, m.id, l.position, l.id", (course_id,))


def my_courses(u):
    ids = core.course_ids_for(u)
    if not ids:
        return []
    qm = ",".join("?" * len(ids))
    cs = db.q(f"SELECT * FROM courses WHERE id IN ({qm}) AND (published=1 OR ?='admin') ORDER BY position, id", (*ids, u["role"]))
    for c in cs:
        c["total"], c["done"], c["pct"] = course_progress(u["id"], c["id"])
    return cs


def my_mentorias(u, upcoming=True, limit=None):
    if u["role"] == "admin":
        rows = db.q("SELECT * FROM mentorias ORDER BY starts_at")
    else:
        rows = db.q("SELECT m.* FROM mentorias m JOIN mentoria_participants p ON p.mentoria_id=m.id WHERE p.user_id=? ORDER BY m.starts_at", (u["id"],))
    for m in rows:
        m["state"] = core.mentoria_state(m)
        m["can_join"] = core.can_join_mentoria(u, m)[0]
        m["parts"] = db.q("SELECT u.id,u.name FROM mentoria_participants p JOIN users u ON u.id=p.user_id WHERE p.mentoria_id=?", (m["id"],))
        m["start_dt"] = core.parse(m["starts_at"]).astimezone(core.TZ)
    if upcoming:
        rows = [m for m in rows if m["state"] in ("agendada", "entrada_liberada", "em_andamento")]
    return rows[:limit] if limit else rows


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    u = me(request)
    cs = my_courses(u)
    cont = None
    last = db.one("SELECT p.lesson_id, p.seconds, l.title, m.course_id, c.title AS ctitle, c.slug FROM progress p JOIN lessons l ON l.id=p.lesson_id "
                  "JOIN modules m ON m.id=l.module_id JOIN courses c ON c.id=m.course_id WHERE p.user_id=? AND p.completed=0 ORDER BY p.updated_at DESC LIMIT 1", (u["id"],))
    if last and last["course_id"] in core.course_ids_for(u):
        cont = last
    elif cs:
        ls = [l for l in lessons_of(cs[0]["id"]) if l["video_status"] == "ready"]
        if ls:
            cont = {"lesson_id": ls[0]["id"], "seconds": 0, "title": ls[0]["title"], "ctitle": cs[0]["title"], "slug": cs[0]["slug"]}
    st = extras.evaluate(u)
    medalhas = db.one("SELECT COUNT(*) n FROM badges_earned WHERE user_id=?", (u["id"],))["n"]
    return page(request, "home.html", u, courses=cs, cont=cont, mentorias=my_mentorias(u, True, 3), n_assets=len(visible_assets(u)),
                xp=extras.xp_info(u, st), medalhas=medalhas)


@app.get("/cursos", response_class=HTMLResponse)
def cursos(request: Request):
    u = me(request)
    return page(request, "cursos.html", u, courses=my_courses(u))


@app.get("/curso/{slug}", response_class=HTMLResponse)
def curso(request: Request, slug: str):
    u = me(request)
    c = db.one("SELECT * FROM courses WHERE slug=?", (slug,))
    if not c or not core.has_entitlement(u, "curso", c["id"]) or (not c["published"] and u["role"] != "admin"):
        raise HTTPException(404, "Curso não encontrado.")
    ls = lessons_of(c["id"])
    prog = {r["lesson_id"]: r for r in db.q("SELECT * FROM progress WHERE user_id=?", (u["id"],))}
    mods = {}
    for l in ls:
        mods.setdefault(l["module_title"], []).append(l)
    total, done, pct = course_progress(u["id"], c["id"])
    nxt = next((l for l in ls if l["video_status"] == "ready" and not prog.get(l["id"], {}).get("completed")), None) or next((l for l in ls if l["video_status"] == "ready"), None)
    assets = [a for a in db.q("SELECT * FROM assets WHERE course_id=? ORDER BY id", (c["id"],))]
    return page(request, "curso.html", u, c=c, mods=mods, prog=prog, total=total, done=done, pct=pct, nxt=nxt, assets=assets)


@app.get("/aula/{lid}", response_class=HTMLResponse)
def aula(request: Request, lid: int):
    u = me(request)
    l = db.one("SELECT l.*, m.course_id, m.title AS module_title FROM lessons l JOIN modules m ON m.id=l.module_id WHERE l.id=?", (lid,))
    if not l or not core.has_entitlement(u, "curso", l["course_id"]):
        raise HTTPException(404, "Aula não encontrada.")
    c = db.one("SELECT * FROM courses WHERE id=?", (l["course_id"],))
    if not c["published"] and u["role"] != "admin":
        raise HTTPException(404, "Aula não encontrada.")
    ls = lessons_of(c["id"])
    idx = next(i for i, x in enumerate(ls) if x["id"] == lid)
    prog = {r["lesson_id"]: r for r in db.q("SELECT * FROM progress WHERE user_id=?", (u["id"],))}
    return page(request, "aula.html", u, l=l, c=c, ls=ls, prev=ls[idx - 1] if idx else None, nxt=ls[idx + 1] if idx + 1 < len(ls) else None,
                prog=prog, start=int(prog.get(lid, {}).get("seconds", 0)))


@app.get("/media/{lid}/{fname}")
def media_file(request: Request, lid: int, fname: str):
    """A cada requisicao (playlist e segmentos) o acesso e revalidado; o arquivo nunca tem URL publica."""
    u = core.user_from_token(request.cookies.get("sid"))
    if not u:
        raise HTTPException(401, "login_required")
    if fname not in ("index.m3u8", "poster.jpg") and not (fname.startswith("seg_") and fname.endswith(".ts") and "/" not in fname):
        raise HTTPException(404, "not_found")
    l = db.one("SELECT l.video_status, m.course_id FROM lessons l JOIN modules m ON m.id=l.module_id WHERE l.id=?", (lid,))
    if not l or l["video_status"] != "ready" or not core.has_entitlement(u, "curso", l["course_id"]):
        raise HTTPException(403, "forbidden")
    p = os.path.join(media.MEDIA, str(lid), fname)
    if not os.path.isfile(p):
        raise HTTPException(404, "not_found")
    ctype = {"m3u8": "application/vnd.apple.mpegurl", "ts": "video/mp2t", "jpg": "image/jpeg"}[fname.rsplit(".", 1)[1]]
    return FileResponse(p, media_type=ctype, headers={"Cache-Control": "private, no-store"})


@app.post("/api/progress")
async def api_progress(request: Request):
    u = me(request)
    if not core.csrf_ok(u, request.headers.get("x-csrf")):
        raise HTTPException(403, "csrf")
    b = await request.json()
    lid = int(b.get("lesson_id", 0))
    l = db.one("SELECT m.course_id, l.duration FROM lessons l JOIN modules m ON m.id=l.module_id WHERE l.id=?", (lid,))
    if not l or not core.has_entitlement(u, "curso", l["course_id"]):
        raise HTTPException(403, "forbidden")
    sec = max(0.0, float(b.get("seconds", 0)))
    done = 1 if (b.get("completed") or (l["duration"] and sec >= 0.92 * l["duration"])) else 0
    old = db.one("SELECT completed FROM progress WHERE user_id=? AND lesson_id=?", (u["id"], lid))
    done = max(done, old["completed"] if old else 0)
    db.x("INSERT INTO progress(user_id,lesson_id,seconds,completed,updated_at) VALUES (?,?,?,?,?) "
         "ON CONFLICT(user_id,lesson_id) DO UPDATE SET seconds=excluded.seconds, completed=excluded.completed, updated_at=excluded.updated_at",
         (u["id"], lid, sec, done, core.iso(core.now())))
    return {"ok": True, "completed": bool(done)}


# ---------------------------------------------------------------- aluno: downloads
def visible_assets(u):
    return [a for a in db.q("SELECT a.*, c.title AS ctitle FROM assets a LEFT JOIN courses c ON c.id=a.course_id ORDER BY a.id DESC") if core.can_see_asset(u, a)]


@app.get("/downloads", response_class=HTMLResponse)
def downloads(request: Request):
    u = me(request)
    return page(request, "downloads.html", u, assets=visible_assets(u))


@app.get("/download/{aid}")
def download(request: Request, aid: int):
    u = me(request)
    a = db.one("SELECT * FROM assets WHERE id=?", (aid,))
    if not a or not core.can_see_asset(u, a):
        raise HTTPException(404, "Arquivo não encontrado.")
    core.audit(u["id"], "download", f"asset:{aid}", a["filename"])
    return FileResponse(a["path"], filename=a["filename"], headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------- aluno: mentorias
@app.get("/mentorias", response_class=HTMLResponse)
def mentorias(request: Request):
    u = me(request)
    todas = my_mentorias(u, upcoming=False)
    prox = [m for m in todas if m["state"] in ("agendada", "entrada_liberada", "em_andamento")]
    pass_ = [m for m in todas if m["state"] in ("concluida", "cancelada")][::-1]
    return page(request, "mentorias.html", u, prox=prox, passadas=pass_)


@app.get("/sala/{mid}", response_class=HTMLResponse)
def sala(request: Request, mid: int):
    u = me(request)
    m = db.one("SELECT * FROM mentorias WHERE id=?", (mid,))
    if not m:
        raise HTTPException(404, "Mentoria não encontrada.")
    ok, why = core.can_join_mentoria(u, m)
    msgs = {"not_participant": "Você não está na lista desta mentoria.", "session_cancelled": "Esta mentoria foi cancelada.",
            "session_not_open": "A sala ainda não está aberta (ou já foi encerrada)."}
    if not ok:
        return page(request, "sala_fechada.html", u, m=m, why=msgs.get(why, why), code=why, start=core.fmt_local(m["starts_at"]))
    consent = db.one("SELECT 1 AS ok FROM consents WHERE mentoria_id=? AND user_id=? AND version=?", (mid, u["id"], CONSENT_VERSION))
    need = m["record_policy"] != "nao" and not consent
    return page(request, "sala.html", u, m=m, need_consent=need, ice=json.dumps(extras.ice_servers(u, ICE)), start=core.fmt_local(m["starts_at"]))


@app.post("/sala/{mid}/consentimento")
async def consent(request: Request, mid: int):
    u = me(request)
    await form(request, u)
    m = db.one("SELECT * FROM mentorias WHERE id=?", (mid,))
    if not m or not core.can_join_mentoria(u, m)[0]:
        raise HTTPException(403, "Sala indisponível.")
    db.x("INSERT INTO consents(mentoria_id,user_id,version,accepted_at) VALUES (?,?,?,?)", (mid, u["id"], CONSENT_VERSION, core.iso(core.now())))
    core.audit(u["id"], "consent", f"mentoria:{mid}", CONSENT_VERSION)
    return RedirectResponse(f"/sala/{mid}", 303)


ROOMS = {}  # mentoria_id -> {user_id: {"ws": WebSocket, "name": str}}


@app.websocket("/ws/sala/{mid}")
async def ws_sala(ws: WebSocket, mid: int):
    origin = ws.headers.get("origin", "")
    host = ws.headers.get("host", "")
    await ws.accept()
    if origin and origin.split("://", 1)[-1] != host:
        await ws.send_json({"type": "denied", "reason": "origin"})
        return await ws.close(code=4403)
    u = core.user_from_token(ws.cookies.get("sid"))
    m = db.one("SELECT * FROM mentorias WHERE id=?", (mid,))
    if not u or not m:
        await ws.send_json({"type": "denied", "reason": "login_required"})
        return await ws.close(code=4401)
    ok, why = core.can_join_mentoria(u, m)
    if ok and m["record_policy"] != "nao" and not db.one("SELECT 1 AS ok FROM consents WHERE mentoria_id=? AND user_id=? AND version=?", (mid, u["id"], CONSENT_VERSION)):
        ok, why = False, "consent_required"
    room = ROOMS.setdefault(mid, {})
    if ok and u["id"] not in room and len(room) >= m["max_participants"]:
        ok, why = False, "room_full"
    if not ok:
        await ws.send_json({"type": "denied", "reason": why})
        return await ws.close(code=4403)
    uid = str(u["id"])
    old = room.get(uid)
    if old:
        try:
            await old["ws"].send_json({"type": "replaced"})
            await old["ws"].close()
        except Exception:
            pass
    peers = [{"id": k, "name": v["name"]} for k, v in room.items() if k != uid]
    room[uid] = {"ws": ws, "name": u["name"]}
    await ws.send_json({"type": "welcome", "you": uid, "name": u["name"], "peers": peers})
    await broadcast(room, {"type": "peer-joined", "id": uid, "name": u["name"]}, skip=uid)
    core.audit(u["id"], "sala_join", f"mentoria:{mid}")
    if u["role"] != "admin":
        extras.record_attendance(mid, u["id"])
    try:
        while True:
            msg = await ws.receive_json()
            t = msg.get("type")
            if t == "signal" and str(msg.get("to")) in room:
                await room[str(msg["to"])]["ws"].send_json({"type": "signal", "from": uid, "data": msg.get("data")})
            elif t == "chat":
                text = str(msg.get("text", ""))[:500].strip()
                if text:
                    await broadcast(room, {"type": "chat", "from": uid, "name": u["name"], "text": text, "ts": core.iso(core.now())})
            elif t == "state":
                await broadcast(room, {"type": "state", "id": uid, "mic": bool(msg.get("mic")), "cam": bool(msg.get("cam")), "screen": bool(msg.get("screen"))}, skip=uid)
    except (WebSocketDisconnect, RuntimeError):
        pass
    except Exception:
        pass
    finally:
        if room.get(uid, {}).get("ws") is ws:
            room.pop(uid, None)
            await broadcast(room, {"type": "peer-left", "id": uid})
        if not room:
            ROOMS.pop(mid, None)


async def broadcast(room, msg, skip=None):
    for k, v in list(room.items()):
        if k != skip:
            try:
                await v["ws"].send_json(msg)
            except Exception:
                pass


@app.get("/gravacoes", response_class=HTMLResponse)
def gravacoes(request: Request):
    u = me(request)
    if u["role"] == "admin":
        rows = db.q("SELECT r.*, m.title AS mtitle, m.starts_at FROM recordings r JOIN mentorias m ON m.id=r.mentoria_id ORDER BY r.id DESC")
    else:
        rows = db.q("SELECT r.*, m.title AS mtitle, m.starts_at FROM recordings r JOIN mentorias m ON m.id=r.mentoria_id "
                    "JOIN recording_grants g ON g.recording_id=r.id WHERE g.user_id=? AND g.revoked_at IS NULL ORDER BY r.id DESC", (u["id"],))
    return page(request, "gravacoes.html", u, recs=rows)


def can_see_recording(u, rid):
    if u["role"] == "admin":
        return True
    return bool(db.one("SELECT 1 AS ok FROM recording_grants WHERE recording_id=? AND user_id=? AND revoked_at IS NULL", (rid, u["id"])))


@app.get("/gravacao/{rid}", response_class=HTMLResponse)
def gravacao_page(request: Request, rid: int):
    u = me(request)
    r = db.one("SELECT r.*, m.title AS mtitle FROM recordings r JOIN mentorias m ON m.id=r.mentoria_id WHERE r.id=?", (rid,))
    if not r or not can_see_recording(u, rid):
        raise HTTPException(404, "Gravação não encontrada.")
    return page(request, "gravacao.html", u, r=r)


@app.get("/gravacao/{rid}/arquivo")
def gravacao_file(request: Request, rid: int):
    u = me(request)
    r = db.one("SELECT * FROM recordings WHERE id=?", (rid,))
    if not r or not can_see_recording(u, rid):
        raise HTTPException(404, "Gravação não encontrada.")
    core.audit(u["id"], "recording_play", f"recording:{rid}")
    return FileResponse(r["path"], media_type=mimetypes.guess_type(r["filename"])[0] or "video/mp4", headers={"Cache-Control": "private, no-store"})


# ---------------------------------------------------------------- perfil
@app.get("/perfil", response_class=HTMLResponse)
def perfil(request: Request):
    u = me(request)
    return page(request, "perfil.html", u, troca=request.query_params.get("troca"))


@app.post("/perfil/senha")
async def perfil_senha(request: Request):
    u = me(request)
    f = await form(request, u)
    row = db.one("SELECT pw_hash FROM users WHERE id=?", (u["id"],))
    if not core.check_pw(f.get("atual") or "", row["pw_hash"]):
        return back("/perfil", erro="Senha atual incorreta.")
    nova = f.get("nova") or ""
    if len(nova) < 10 or nova != f.get("confirma"):
        return back("/perfil", erro="A nova senha precisa ter 10 ou mais caracteres e ser igual à confirmação.")
    db.x("UPDATE users SET pw_hash=?, must_change=0 WHERE id=?", (core.hash_pw(nova), u["id"]))
    db.x("DELETE FROM sessions WHERE user_id=? AND token<>?", (u["id"], request.cookies.get("sid")))
    core.audit(u["id"], "password_change", f"user:{u['id']}")
    return back("/", ok="Senha atualizada.")


# ---------------------------------------------------------------- admin
@app.get("/admin", response_class=HTMLResponse)
def admin(request: Request):
    u = me(request, admin=True)
    s = {"alunos": db.one("SELECT COUNT(*) n FROM users WHERE role='aluno' AND active=1")["n"],
         "cursos": db.one("SELECT COUNT(*) n FROM courses")["n"],
         "aulas": db.one("SELECT COUNT(*) n FROM lessons WHERE video_status='ready'")["n"],
         "processando": db.one("SELECT COUNT(*) n FROM lessons WHERE video_status='processing'")["n"],
         "ativos": db.one("SELECT COUNT(*) n FROM assets")["n"]}
    return page(request, "admin.html", u, s=s, mentorias=my_mentorias(u, True, 4), recentes=db.q(
        "SELECT a.*, u.name AS actor FROM audit a LEFT JOIN users u ON u.id=a.actor_id ORDER BY a.id DESC LIMIT 8"))


@app.get("/admin/alunos", response_class=HTMLResponse)
def admin_alunos(request: Request):
    u = me(request, admin=True)
    rows = db.q("SELECT * FROM users ORDER BY role, name")
    for r in rows:
        r["ncursos"] = db.one("SELECT COUNT(*) n FROM entitlements WHERE user_id=? AND resource_type='curso' AND status='active'", (r["id"],))["n"]
    return page(request, "admin_alunos.html", u, rows=rows, cursos_all=db.q("SELECT id,title FROM courses ORDER BY title"))


@app.post("/admin/alunos")
async def admin_alunos_criar(request: Request):
    u = me(request, admin=True)
    f = await form(request, u)
    email = (f.get("email") or "").strip().lower()
    name = (f.get("name") or "").strip()
    if not core.EMAIL_RE.match(email) or not name:
        return back("/admin/alunos", erro="Informe nome e e-mail válidos.")
    if db.one("SELECT 1 AS ok FROM users WHERE email=?", (email,)):
        return back("/admin/alunos", erro="Já existe um usuário com esse e-mail.")
    pw = (f.get("password") or "").strip() or secrets.token_urlsafe(9)
    if len(pw) < 10:
        return back("/admin/alunos", erro="A senha precisa ter 10 ou mais caracteres.")
    role = "mentor" if f.get("role") == "mentor" else "aluno"
    uid = db.x("INSERT INTO users(email,name,role,pw_hash,must_change,created_at) VALUES (?,?,?,?,?,?)",
               (email, name, role, core.hash_pw(pw), 1 if f.get("must_change") else 0, core.iso(core.now())))
    for cid in f.getlist("cursos"):
        core.grant(uid, "curso", int(cid), "manual_grant", u["id"], "criação do aluno")
    core.audit(u["id"], "user_create", f"user:{uid}", email)
    return page(request, "admin_aluno_criado.html", u, nome=name, email=email, senha=pw)


@app.get("/admin/alunos/{uid}", response_class=HTMLResponse)
def admin_aluno(request: Request, uid: int):
    u = me(request, admin=True)
    a = db.one("SELECT * FROM users WHERE id=?", (uid,))
    if not a:
        raise HTTPException(404, "Usuário não encontrado.")
    ents = db.q("SELECT e.*, CASE e.resource_type WHEN 'curso' THEN (SELECT title FROM courses WHERE id=e.resource_id) "
                "WHEN 'ativo' THEN (SELECT title FROM assets WHERE id=e.resource_id) END AS rtitle FROM entitlements e WHERE user_id=? ORDER BY id DESC", (uid,))
    return page(request, "admin_aluno.html", u, a=a, ents=ents, courses=db.q("SELECT id,title FROM courses ORDER BY title"),
                assets=db.q("SELECT id,title FROM assets ORDER BY title"), logs=db.q("SELECT * FROM audit WHERE target LIKE ? ORDER BY id DESC LIMIT 15", (f"user:{uid}%",)))


@app.post("/admin/alunos/{uid}/acesso")
async def admin_acesso(request: Request, uid: int):
    u = me(request, admin=True)
    f = await form(request, u)
    rtype, rid = f.get("recurso", "curso:0").split(":")
    reason = (f.get("motivo") or "").strip()
    if not reason:
        return back(f"/admin/alunos/{uid}", erro="Informe o motivo da concessão (fica na auditoria).")
    ends = core.local_to_utc(f["ate"]) if f.get("ate") else None
    core.grant(uid, rtype, int(rid), "manual_grant", u["id"], reason, ends)
    return back(f"/admin/alunos/{uid}", ok="Acesso concedido.")


@app.post("/admin/alunos/{uid}/revogar/{eid}")
async def admin_revogar(request: Request, uid: int, eid: int):
    u = me(request, admin=True)
    f = await form(request, u)
    core.revoke(eid, u["id"], (f.get("motivo") or "revogado pelo administrador"))
    return back(f"/admin/alunos/{uid}", ok="Acesso revogado.")


@app.post("/admin/alunos/{uid}/conta")
async def admin_conta(request: Request, uid: int):
    u = me(request, admin=True)
    f = await form(request, u)
    acao = f.get("acao")
    if uid == u["id"] and acao == "desativar":
        return back(f"/admin/alunos/{uid}", erro="Você não pode desativar a própria conta.")
    if acao == "desativar":
        db.x("UPDATE users SET active=0 WHERE id=?", (uid,))
        db.x("DELETE FROM sessions WHERE user_id=?", (uid,))
    elif acao == "ativar":
        db.x("UPDATE users SET active=1 WHERE id=?", (uid,))
    elif acao == "senha":
        pw = secrets.token_urlsafe(9)
        db.x("UPDATE users SET pw_hash=?, must_change=1 WHERE id=?", (core.hash_pw(pw), uid))
        db.x("DELETE FROM sessions WHERE user_id=?", (uid,))
        a = db.one("SELECT name,email FROM users WHERE id=?", (uid,))
        core.audit(u["id"], "password_reset", f"user:{uid}")
        return page(request, "admin_aluno_criado.html", u, nome=a["name"], email=a["email"], senha=pw, reset=True)
    core.audit(u["id"], f"user_{acao}", f"user:{uid}")
    return back(f"/admin/alunos/{uid}", ok="Conta atualizada.")


@app.get("/admin/cursos", response_class=HTMLResponse)
def admin_cursos(request: Request):
    u = me(request, admin=True)
    rows = db.q("SELECT c.*, (SELECT COUNT(*) FROM lessons l JOIN modules m ON m.id=l.module_id WHERE m.course_id=c.id) AS naulas FROM courses c ORDER BY position, id")
    return page(request, "admin_cursos.html", u, rows=rows)


@app.post("/admin/cursos")
async def admin_curso_criar(request: Request):
    u = me(request, admin=True)
    f = await form(request, u)
    title = (f.get("title") or "").strip()
    if not title:
        return back("/admin/cursos", erro="Informe o título.")
    slug = core.slugify(title)
    if db.one("SELECT 1 AS ok FROM courses WHERE slug=?", (slug,)):
        slug += "-" + secrets.token_hex(2)
    cid = db.x("INSERT INTO courses(slug,title,subtitle,description,created_at) VALUES (?,?,?,?,?)",
               (slug, title, (f.get("subtitle") or "").strip(), (f.get("description") or "").strip(), core.iso(core.now())))
    core.audit(u["id"], "course_create", f"course:{cid}", title)
    return RedirectResponse(f"/admin/cursos/{cid}", 303)


@app.get("/admin/cursos/{cid}", response_class=HTMLResponse)
def admin_curso(request: Request, cid: int):
    u = me(request, admin=True)
    c = db.one("SELECT * FROM courses WHERE id=?", (cid,))
    if not c:
        raise HTTPException(404, "Curso não encontrado.")
    mods = db.q("SELECT * FROM modules WHERE course_id=? ORDER BY position, id", (cid,))
    for m in mods:
        m["lessons"] = db.q("SELECT * FROM lessons WHERE module_id=? ORDER BY position, id", (m["id"],))
    alunos = db.q("SELECT u.id,u.name,u.email FROM entitlements e JOIN users u ON u.id=e.user_id WHERE e.resource_type='curso' AND e.resource_id=? AND e.status='active' ORDER BY u.name", (cid,))
    todos = db.q("SELECT id,name,email FROM users WHERE role<>'admin' AND active=1 ORDER BY name")
    kp = db.one("SELECT kiwify_id, support FROM kiwify_products WHERE course_id=?", (cid,))
    return page(request, "admin_curso.html", u, c=c, mods=mods, alunos=alunos, todos=todos, kiwify=kp["kiwify_id"] if kp else "",
                kiwify_suporte=bool(kp and kp["support"]),
                ativos=db.q("SELECT * FROM assets WHERE course_id=?", (cid,)))


@app.post("/admin/cursos/{cid}")
async def admin_curso_salvar(request: Request, cid: int):
    u = me(request, admin=True)
    f = await form(request, u)
    acao = f.get("acao")
    if acao == "dados":
        db.x("UPDATE courses SET title=?, subtitle=?, description=?, published=? WHERE id=?",
             ((f.get("title") or "").strip(), (f.get("subtitle") or "").strip(), (f.get("description") or "").strip(), 1 if f.get("published") else 0, cid))
    elif acao == "modulo":
        pos = db.one("SELECT COALESCE(MAX(position),0)+1 p FROM modules WHERE course_id=?", (cid,))["p"]
        db.x("INSERT INTO modules(course_id,title,position) VALUES (?,?,?)", (cid, (f.get("title") or "Novo módulo").strip(), pos))
    elif acao == "aula":
        mid = int(f.get("module_id"))
        pos = db.one("SELECT COALESCE(MAX(position),0)+1 p FROM lessons WHERE module_id=?", (mid,))["p"]
        db.x("INSERT INTO lessons(module_id,title,description,position,created_at) VALUES (?,?,?,?,?)",
             (mid, (f.get("title") or "Nova aula").strip(), (f.get("description") or "").strip(), pos, core.iso(core.now())))
    elif acao == "matricular":
        core.grant(int(f.get("user_id")), "curso", cid, "manual_grant", u["id"], (f.get("motivo") or "matrícula pelo painel"))
    elif acao == "kiwify":
        kid = (f.get("kiwify_id") or "").strip()
        db.x("DELETE FROM kiwify_products WHERE course_id=?", (cid,))
        if kid:
            db.x("INSERT OR REPLACE INTO kiwify_products(kiwify_id, course_id, support) VALUES (?,?,?)", (kid, cid, 1 if f.get("suporte") else 0))
    core.audit(u["id"], f"course_{acao}", f"course:{cid}")
    return back(f"/admin/cursos/{cid}", ok="Salvo.")


@app.post("/admin/aulas/{lid}/video")
async def admin_video(request: Request, lid: int):
    u = me(request, admin=True)
    f = await form(request, u)
    up = f.get("video")
    l = db.one("SELECT l.*, m.course_id FROM lessons l JOIN modules m ON m.id=l.module_id WHERE l.id=?", (lid,))
    if not l or not up or not getattr(up, "filename", ""):
        return back("/admin/cursos", erro="Aula ou arquivo ausente.")
    ext = os.path.splitext(up.filename)[1].lower()
    if ext not in (".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"):
        return back(f"/admin/cursos/{l['course_id']}", erro="Formato não suportado (use mp4, mov, mkv, webm).")
    dest = os.path.join(media.SRC, f"{lid}{ext}")
    for old in os.listdir(media.SRC):
        if old.split(".")[0] == str(lid):
            os.remove(os.path.join(media.SRC, old))
    await save_upload(up, dest)
    media.enqueue(lid, dest)
    core.audit(u["id"], "video_upload", f"lesson:{lid}", up.filename)
    return back(f"/admin/cursos/{l['course_id']}", ok="Vídeo recebido, processando.")


@app.post("/admin/aulas/{lid}/excluir")
async def admin_aula_excluir(request: Request, lid: int):
    u = me(request, admin=True)
    await form(request, u)
    l = db.one("SELECT m.course_id FROM lessons l JOIN modules m ON m.id=l.module_id WHERE l.id=?", (lid,))
    if l:
        db.x("DELETE FROM lessons WHERE id=?", (lid,))
        shutil.rmtree(os.path.join(media.MEDIA, str(lid)), ignore_errors=True)
        core.audit(u["id"], "lesson_delete", f"lesson:{lid}")
        return back(f"/admin/cursos/{l['course_id']}", ok="Aula removida.")
    return back("/admin/cursos")


@app.get("/admin/ativos", response_class=HTMLResponse)
def admin_ativos(request: Request):
    u = me(request, admin=True)
    return page(request, "admin_ativos.html", u, rows=db.q("SELECT a.*, c.title AS ctitle FROM assets a LEFT JOIN courses c ON c.id=a.course_id ORDER BY a.id DESC"),
                courses=db.q("SELECT id,title FROM courses ORDER BY title"))


@app.post("/admin/ativos")
async def admin_ativos_up(request: Request):
    u = me(request, admin=True)
    f = await form(request, u)
    up = f.get("arquivo")
    title = (f.get("title") or "").strip()
    if not up or not getattr(up, "filename", "") or not title:
        return back("/admin/ativos", erro="Informe título e arquivo.")
    fname = os.path.basename(up.filename).replace("\x00", "")
    folder = os.path.join(db.DATA, "assets", secrets.token_hex(6))
    os.makedirs(folder)
    dest = os.path.join(folder, fname)
    size = await save_upload(up, dest)
    cid = int(f["course_id"]) if f.get("course_id") else None
    aid = db.x("INSERT INTO assets(course_id,title,description,filename,path,size,created_at) VALUES (?,?,?,?,?,?,?)",
               (cid, title, (f.get("description") or "").strip(), fname, dest, size, core.iso(core.now())))
    core.audit(u["id"], "asset_create", f"asset:{aid}", title)
    return back("/admin/ativos", ok="Arquivo publicado.")


@app.get("/admin/mentorias", response_class=HTMLResponse)
def admin_mentorias(request: Request):
    u = me(request, admin=True)
    return page(request, "admin_mentorias.html", u, rows=my_mentorias(u, upcoming=False)[::-1], alunos=db.q("SELECT id,name,email FROM users WHERE role='aluno' AND active=1 ORDER BY name"))


@app.post("/admin/mentorias")
async def admin_mentoria_criar(request: Request):
    u = me(request, admin=True)
    f = await form(request, u)
    try:
        starts = core.local_to_utc(f["starts"])
        dur = max(15, min(240, int(f.get("duration") or 60)))
        ob = max(0, min(120, int(f.get("open_before") or 10)))
        ca = max(0, min(240, int(f.get("close_after") or 30)))
    except (KeyError, ValueError):
        return back("/admin/mentorias", erro="Data ou duração inválida.")
    ids = [int(i) for i in f.getlist("alunos")]
    if not (f.get("title") or "").strip() or not ids:
        return back("/admin/mentorias", erro="Informe título e ao menos um participante.")
    if len(ids) + 1 > 5:
        return back("/admin/mentorias", erro="Máximo de 5 participantes (incluindo o mentor).")
    # conflito: o mentor nao pode ter duas sessoes sobrepostas
    ini = core.parse(starts)
    fim = ini + timedelta(minutes=dur)
    for o in db.q("SELECT * FROM mentorias WHERE mentor_id=? AND status<>'cancelada'", (u["id"],)):
        os_, oe = core.parse(o["starts_at"]), core.parse(o["starts_at"]) + timedelta(minutes=o["duration_min"])
        if ini < oe and os_ < fim:
            return back("/admin/mentorias", erro=f"Conflito com “{o['title']}” em {core.fmt_local(o['starts_at'])}.")
    mid = db.x("INSERT INTO mentorias(title,description,mentor_id,starts_at,duration_min,open_before_min,close_after_min,record_policy,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
               (f["title"].strip(), (f.get("description") or "").strip(), u["id"], starts, dur, ob, ca, "manual" if f.get("gravar") else "nao", core.iso(core.now())))
    for i in ids:
        db.x("INSERT OR IGNORE INTO mentoria_participants(mentoria_id,user_id) VALUES (?,?)", (mid, i))
    core.audit(u["id"], "mentoria_create", f"mentoria:{mid}", f["title"].strip())
    return back("/admin/mentorias", ok="Mentoria agendada.")


@app.post("/admin/mentorias/{mid}/cancelar")
async def admin_mentoria_cancelar(request: Request, mid: int):
    u = me(request, admin=True)
    await form(request, u)
    db.x("UPDATE mentorias SET status='cancelada' WHERE id=?", (mid,))
    core.audit(u["id"], "mentoria_cancel", f"mentoria:{mid}")
    return back("/admin/mentorias", ok="Mentoria cancelada.")


@app.post("/admin/mentorias/{mid}/gravacao")
async def admin_gravacao(request: Request, mid: int):
    u = me(request, admin=True)
    f = await form(request, u)
    up = f.get("arquivo")
    if not up or not getattr(up, "filename", ""):
        return back("/admin/mentorias", erro="Arquivo ausente.")
    fname = os.path.basename(up.filename)
    folder = os.path.join(db.DATA, "recordings", secrets.token_hex(6))
    os.makedirs(folder)
    dest = os.path.join(folder, fname)
    size = await save_upload(up, dest)
    rid = db.x("INSERT INTO recordings(mentoria_id,title,filename,path,size,created_at) VALUES (?,?,?,?,?,?)",
               (mid, (f.get("title") or fname).strip(), fname, dest, size, core.iso(core.now())))
    for p in db.q("SELECT user_id FROM mentoria_participants WHERE mentoria_id=?", (mid,)):
        if f.get("compartilhar"):
            db.x("INSERT OR IGNORE INTO recording_grants(recording_id,user_id,source,granted_at) VALUES (?,?,?,?)", (rid, p["user_id"], "explicit_admin_share", core.iso(core.now())))
    core.audit(u["id"], "recording_create", f"recording:{rid}", f"mentoria:{mid} share={bool(f.get('compartilhar'))}")
    return back("/admin/mentorias", ok="Gravação publicada.")


@app.get("/admin/auditoria", response_class=HTMLResponse)
def admin_auditoria(request: Request):
    u = me(request, admin=True)
    return page(request, "admin_auditoria.html", u, rows=db.q("SELECT a.*, u.name AS actor FROM audit a LEFT JOIN users u ON u.id=a.actor_id ORDER BY a.id DESC LIMIT 200"))


# ---------------------------------------------------------------- Kiwify (webhook de eventos comerciais)
@app.post("/webhooks/kiwify")
async def kiwify(request: Request):
    if not KIWIFY_TOKEN:
        raise HTTPException(503, "webhook_disabled")
    raw = await request.body()
    sig = request.query_params.get("signature", "")
    expected = hmac.new(KIWIFY_TOKEN.encode(), raw, hashlib.sha1).hexdigest()
    if not hmac.compare_digest(sig, expected):
        core.audit(None, "webhook_invalid", "kiwify")
        raise HTTPException(401, "invalid_signature")
    ev = json.loads(raw or b"{}")
    status = ev.get("order_status", "")
    eid = f"{ev.get('order_id', '')}:{status}"
    if db.one("SELECT 1 AS ok FROM webhook_events WHERE event_id=?", (eid,)):
        return {"ok": True, "duplicate": True}
    prod = (ev.get("Product") or {}).get("product_id", "")
    cust = ev.get("Customer") or {}
    email = (cust.get("email") or "").strip().lower()
    mapa = db.one("SELECT * FROM kiwify_products WHERE kiwify_id=?", (prod,))
    result = "ignored"
    if mapa and core.EMAIL_RE.match(email):
        u = db.one("SELECT * FROM users WHERE email=?", (email,))
        if status == "paid":
            if not u:
                uid = db.x("INSERT INTO users(email,name,role,pw_hash,must_change,created_at) VALUES (?,?, 'aluno', ?, 1, ?)",
                           (email, cust.get("full_name") or email.split("@")[0], core.hash_pw(secrets.token_urlsafe(16)), core.iso(core.now())))
            else:
                uid = u["id"]
            if mapa["course_id"]:
                core.grant(uid, "curso", mapa["course_id"], "kiwify_purchase", None, f"pedido {ev.get('order_id')}")
            if mapa["support"]:
                core.grant(uid, "suporte", 0, "kiwify_purchase", None, f"pedido {ev.get('order_id')}")
            result = "granted"
        elif status in ("refunded", "chargedback") and u and mapa["course_id"]:
            for e in db.q("SELECT id FROM entitlements WHERE user_id=? AND resource_type='curso' AND resource_id=? AND source='kiwify_purchase' AND status='active'", (u["id"], mapa["course_id"])):
                core.revoke(e["id"], None, f"{status} pedido {ev.get('order_id')}")
            if mapa["support"]:
                for e in db.q("SELECT id FROM entitlements WHERE user_id=? AND resource_type='suporte' AND source='kiwify_purchase' AND status='active'", (u["id"],)):
                    core.revoke(e["id"], None, f"{status} pedido {ev.get('order_id')}")
            result = "revoked"
    db.x("INSERT INTO webhook_events(event_id,kind,received_at,status) VALUES (?,?,?,?)", (eid, status, core.iso(core.now()), result))
    return {"ok": True, "result": result}


extras.setup(app, me, page, form, back, T)
ciclo.setup(app, me, page, form, back, T, extras)
