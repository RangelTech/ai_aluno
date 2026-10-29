"""Identidade, sessoes, entitlements e auditoria. Backend e a autoridade: nada aqui depende do front-end."""
import hmac
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import bcrypt

from . import db

TZ = ZoneInfo(os.environ.get("APP_TZ", "America/Sao_Paulo"))
SESSION_DAYS = 14
FMT = "%Y-%m-%dT%H:%M:%SZ"


def now():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime(FMT)


def parse(s):
    return datetime.strptime(s, FMT).replace(tzinfo=timezone.utc)


def local_to_utc(s):
    """Valor de <input type=datetime-local> (fuso de Sao Paulo) -> ISO UTC."""
    dt = datetime.strptime(s, "%Y-%m-%dT%H:%M").replace(tzinfo=TZ)
    return iso(dt)


def fmt_local(s, pattern="%d/%m/%Y %H:%M"):
    return parse(s).astimezone(TZ).strftime(pattern) if s else ""


def fmt_dur(sec):
    sec = int(sec or 0)
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}h{m:02d}" if h else f"{m}:{s:02d}"


# ---------- senhas ----------
def hash_pw(pw):
    return bcrypt.hashpw(pw.encode()[:72], bcrypt.gensalt(12)).decode()


def check_pw(pw, h):
    try:
        return bcrypt.checkpw(pw.encode()[:72], h.encode())
    except ValueError:
        return False


# ---------- sessoes ----------
def new_session(user_id):
    tok, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
    db.x("INSERT INTO sessions(token,user_id,csrf,expires_at,created_at) VALUES (?,?,?,?,?)",
         (tok, user_id, csrf, iso(now() + timedelta(days=SESSION_DAYS)), iso(now())))
    db.x("UPDATE users SET last_login=? WHERE id=?", (iso(now()), user_id))
    return tok


def user_from_token(tok):
    if not tok:
        return None
    r = db.one("SELECT u.*, s.csrf AS csrf, s.expires_at AS sexp FROM sessions s JOIN users u ON u.id=s.user_id "
               "WHERE s.token=? AND u.active=1", (tok,))
    if not r or parse(r["sexp"]) < now():
        return None
    return r


def end_session(tok):
    db.x("DELETE FROM sessions WHERE token=?", (tok,))


def csrf_ok(user, given):
    return bool(given) and hmac.compare_digest(user["csrf"], given)


# ---------- limitador de tentativas de login ----------
_fails = {}


def login_blocked(key):
    n, t0 = _fails.get(key, (0, 0))
    if time.time() - t0 > 900:
        _fails.pop(key, None)
        return False
    return n >= 8


def login_fail(key):
    n, t0 = _fails.get(key, (0, time.time()))
    _fails[key] = (n + 1, t0 if n else time.time())


def login_ok(key):
    _fails.pop(key, None)


# ---------- auditoria ----------
def audit(actor_id, action, target="", detail=""):
    db.x("INSERT INTO audit(ts,actor_id,action,target,detail) VALUES (?,?,?,?,?)", (iso(now()), actor_id, action, target, detail[:500]))


# ---------- entitlements ----------
def _active_clause():
    return "status='active' AND (starts_at IS NULL OR starts_at<=:n) AND (ends_at IS NULL OR ends_at>=:n)"


def has_entitlement(user, rtype, rid):
    if user["role"] == "admin":
        return True
    # curso com resource_id 0 = "todos os cursos" (ex.: produto da mentoria que inclui a plataforma inteira)
    r = db.one(f"SELECT 1 AS ok FROM entitlements WHERE user_id=:u AND resource_type=:t AND (resource_id=:r OR (:t='curso' AND resource_id=0)) AND {_active_clause()} LIMIT 1",
               {"u": user["id"], "t": rtype, "r": rid, "n": iso(now())})
    return bool(r)


def can_see_asset(user, asset):
    if user["role"] == "admin":
        return True
    if has_entitlement(user, "ativo", asset["id"]):
        return True
    return bool(asset["course_id"]) and has_entitlement(user, "curso", asset["course_id"])


def grant(user_id, rtype, rid, source, granted_by, reason="", ends_at=None):
    ex = db.one("SELECT id FROM entitlements WHERE user_id=? AND resource_type=? AND resource_id=? AND status='active'", (user_id, rtype, rid))
    if ex:
        return ex["id"]
    eid = db.x("INSERT INTO entitlements(user_id,resource_type,resource_id,source,status,starts_at,ends_at,granted_by,reason,created_at) "
               "VALUES (?,?,?,?, 'active', NULL, ?, ?, ?, ?)", (user_id, rtype, rid, source, ends_at, granted_by, reason, iso(now())))
    audit(granted_by, "grant", f"user:{user_id} {rtype}:{rid}", f"{source} {reason}")
    return eid


def revoke(ent_id, actor_id, reason=""):
    e = db.one("SELECT * FROM entitlements WHERE id=?", (ent_id,))
    if not e:
        return
    db.x("UPDATE entitlements SET status='revoked', reason=? WHERE id=?", (reason or e["reason"], ent_id))
    audit(actor_id, "revoke", f"user:{e['user_id']} {e['resource_type']}:{e['resource_id']}", reason)


def course_ids_for(user):
    if user["role"] == "admin":
        return [r["id"] for r in db.q("SELECT id FROM courses")]
    ids = [r["resource_id"] for r in db.q(f"SELECT resource_id FROM entitlements WHERE user_id=:u AND resource_type='curso' AND {_active_clause()}",
                                          {"u": user["id"], "n": iso(now())})]
    if 0 in ids:
        return [r["id"] for r in db.q("SELECT id FROM courses")]
    return ids


# ---------- mentorias ----------
def mentoria_window(m):
    start = parse(m["starts_at"])
    end = start + timedelta(minutes=m["duration_min"])
    return start - timedelta(minutes=m["open_before_min"]), end + timedelta(minutes=m["close_after_min"]), start, end


def mentoria_state(m):
    if m["status"] == "cancelada":
        return "cancelada"
    o, c, s, e = mentoria_window(m)
    n = now()
    if n < o:
        return "agendada"
    if n <= c:
        return "em_andamento" if s <= n <= e else "entrada_liberada"
    return "concluida"


def can_join_mentoria(user, m):
    if m["status"] == "cancelada":
        return False, "session_cancelled"
    if user["role"] != "admin":
        ok = db.one("SELECT 1 AS ok FROM mentoria_participants WHERE mentoria_id=? AND user_id=?", (m["id"], user["id"]))
        if not ok:
            return False, "not_participant"
    o, c, _, _ = mentoria_window(m)
    n = now()
    if n < o or n > c:
        return False, "session_not_open"
    return True, ""


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def slugify(s):
    s = re.sub(r"[^a-z0-9]+", "-", s.lower().encode("ascii", "ignore").decode()).strip("-")
    return s or secrets.token_hex(3)


def ensure_master():
    """Cria/atualiza o usuario master a partir do ambiente (hash bcrypt; a senha em si fica so no cofre do dono)."""
    email = os.environ.get("MASTER_EMAIL", "").strip().lower()
    h = os.environ.get("MASTER_PASSWORD_HASH", "")
    if not email or not h:
        return
    name = os.environ.get("MASTER_NAME", "Lucas Rangel")
    u = db.one("SELECT id FROM users WHERE email=?", (email,))
    if u:
        db.x("UPDATE users SET pw_hash=?, role='admin', active=1, name=? WHERE id=?", (h, name, u["id"]))
    else:
        db.x("INSERT INTO users(email,name,role,pw_hash,created_at) VALUES (?,?, 'admin', ?, ?)", (email, name, h, iso(now())))
