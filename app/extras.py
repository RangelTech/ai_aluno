"""Conquistas (gamificacao), certificados em PDF, foto de perfil, suporte por WhatsApp e credenciais TURN.
As rotas sao registradas por setup(): este modulo nao importa o main (evita import circular)."""
import base64
import hashlib
import hmac
import io
import os
import secrets
import time
from datetime import timedelta

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

from . import core, db

CERT_MIN_PCT = 80
PUBLIC_URL = os.environ.get("PUBLIC_URL", "https://user.rangeltech.net").rstrip("/")
DEFAULT_WA = os.environ.get("SUPPORT_WHATSAPP", "5562985613482")
DEFAULT_WA_MSG = "Oi, Lucas! Aqui é {nome}, da área do aluno."
MAX_PHOTO = 8 * 1024 * 1024
MESES = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro", "novembro", "dezembro"]

ICONS = {
    "play": '<polygon points="6 4 20 12 6 20 6 4"/>',
    "zap": '<path d="M13 2 3 14h9l-1 8 10-12h-9l1-8z"/>',
    "flame": '<path d="M8.5 14.5A2.5 2.5 0 0 0 11 12c0-1.38-.5-2-1-3-1.07-2.14-.22-4.05 2-6 .5 2.5 2 4.9 4 6.5 2 1.6 3 3.5 3 5.5a7 7 0 1 1-14 0c0-1.15.43-2.29 1-3a2.5 2.5 0 0 0 2.5 2.5z"/>',
    "calendar": '<rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/>',
    "users": '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/>',
    "award": '<circle cx="12" cy="8" r="6"/><path d="M15.48 12.89 17 22l-5-3-5 3 1.52-9.11"/>',
    "camera": '<path d="M14.5 4h-5L7 7H4a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V9a2 2 0 0 0-2-2h-3l-2.5-3z"/><circle cx="12" cy="13" r="3"/>',
    "trophy": '<path d="M6 9H4.5a2.5 2.5 0 0 1 0-5H6M18 9h1.5a2.5 2.5 0 0 0 0-5H18M4 22h16M10 14.66V17c0 .55-.47.98-.97 1.21C7.85 18.75 7 20.24 7 22M14 14.66V17c0 .55.47.98.97 1.21C16.15 18.75 17 20.24 17 22M18 2H6v7a6 6 0 0 0 12 0V2z"/>',
    "chat": '<path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/>',
    "file": '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6M9 15l2 2 4-4"/>',
}

# (id, titulo, descricao, icone, regra sobre as estatisticas do aluno)
BADGES = [
    ("primeira-aula", "Primeiro passo", "Concluiu a primeira aula.", "play", lambda s: s["aulas"] >= 1),
    ("cinco-aulas", "Pegando ritmo", "Concluiu 5 aulas.", "zap", lambda s: s["aulas"] >= 5),
    ("vinte-aulas", "Maratonista", "Concluiu 20 aulas.", "flame", lambda s: s["aulas"] >= 20),
    ("tres-dias", "Três dias seguidos", "Estudou em 3 dias seguidos.", "calendar", lambda s: s["sequencia"] >= 3),
    ("sete-dias", "Semana inteira", "Estudou 7 dias seguidos.", "flame", lambda s: s["sequencia"] >= 7),
    ("na-sala", "Na sala", "Participou da primeira mentoria.", "users", lambda s: s["mentorias"] >= 1),
    ("presenca", "Presença constante", "Assistiu a 3 mentorias.", "users", lambda s: s["mentorias"] >= 3),
    ("mentorado", "Mentorado de verdade", "Assistiu a 10 mentorias.", "award", lambda s: s["mentorias"] >= 10),
    ("com-foto", "Com a minha cara", "Colocou a foto no perfil.", "camera", lambda s: s["foto"]),
    ("certificado", "Certificado em mãos", "Emitiu o primeiro certificado.", "award", lambda s: s["certificados"] >= 1),
]


# ---------------------------------------------------------------- configuracoes
def setting(key, default=""):
    r = db.one("SELECT value FROM settings WHERE key=?", (key,))
    return r["value"] if r else default


def set_setting(key, value):
    db.x("INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


# ---------------------------------------------------------------- suporte no WhatsApp
def has_support(user):
    """So quem comprou mentoria/acompanhamento (ou recebeu do admin). Admin nao precisa do botao."""
    if not user:
        return False
    return bool(db.one("SELECT 1 AS ok FROM entitlements WHERE user_id=:u AND resource_type='suporte' AND " + core._active_clause() + " LIMIT 1",
                       {"u": user["id"], "n": core.iso(core.now())}))


def wa_link(user):
    num = "".join(ch for ch in setting("whatsapp_numero", DEFAULT_WA) if ch.isdigit())
    msg = setting("whatsapp_mensagem", DEFAULT_WA_MSG).replace("{nome}", user["name"])
    from urllib.parse import quote
    return f"https://wa.me/{num}?text={quote(msg)}"


# ---------------------------------------------------------------- progresso de curso
def cprog(uid, cid):
    rows = db.q("SELECT l.duration d, COALESCE(p.completed,0) c FROM lessons l JOIN modules m ON m.id=l.module_id "
                "LEFT JOIN progress p ON p.lesson_id=l.id AND p.user_id=? WHERE m.course_id=? AND l.video_status='ready'", (uid, cid))
    tot = len(rows)
    done = sum(r["c"] for r in rows)
    minutes = int(round(sum(r["d"] or 0 for r in rows) / 60))
    return tot, done, (round(100 * done / tot) if tot else 0), minutes


# ---------------------------------------------------------------- gamificacao
def _streak(uid):
    days = set()
    for r in db.q("SELECT updated_at FROM progress WHERE user_id=?", (uid,)):
        try:
            days.add(core.parse(r["updated_at"]).astimezone(core.TZ).date())
        except ValueError:
            pass
    best = run = 0
    prev = None
    for d in sorted(days):
        run = run + 1 if prev and (d - prev).days == 1 else 1
        best = max(best, run)
        prev = d
    return best


def stats(user):
    uid = user["id"]
    ment = {r["mentoria_id"] for r in db.q("SELECT mentoria_id FROM attendance WHERE user_id=?", (uid,))}
    for r in db.q("SELECT DISTINCT rec.mentoria_id FROM audit a JOIN recordings rec ON ('recording:' || rec.id)=a.target "
                  "WHERE a.actor_id=? AND a.action='recording_play'", (uid,)):
        ment.add(r["mentoria_id"])
    concl = []
    for cid in core.course_ids_for(user) if user["role"] != "admin" else []:
        tot, done, pct, _ = cprog(uid, cid)
        if tot and done == tot:
            concl.append(cid)
    return {
        "aulas": db.one("SELECT COUNT(*) n FROM progress p JOIN lessons l ON l.id=p.lesson_id WHERE p.user_id=? AND p.completed=1", (uid,))["n"],
        "sequencia": _streak(uid),
        "mentorias": len(ment),
        "foto": bool(user.get("photo")),
        "certificados": db.one("SELECT COUNT(*) n FROM certificates WHERE user_id=?", (uid,))["n"],
        "cursos_concluidos": concl,
    }


def evaluate(user):
    """Concede (de forma idempotente) o que o aluno ja merece. Devolve as estatisticas."""
    if user["role"] == "admin":
        return None
    s = stats(user)
    ids = [b[0] for b in BADGES if b[4](s)] + [f"curso:{cid}" for cid in s["cursos_concluidos"]]
    have = {r["badge"] for r in db.q("SELECT badge FROM badges_earned WHERE user_id=?", (user["id"],))}
    for b in ids:
        if b not in have:
            db.x("INSERT OR IGNORE INTO badges_earned(user_id,badge,earned_at,seen) VALUES (?,?,?,0)", (user["id"], b, core.iso(core.now())))
    return s


def catalog(user, s=None):
    """Lista completa de conquistas com o estado de cada uma (conquistada ou bloqueada)."""
    got = {r["badge"]: r for r in db.q("SELECT * FROM badges_earned WHERE user_id=?", (user["id"],))}
    out = []
    for bid, title, desc, icon, _ in BADGES:
        out.append({"id": bid, "title": title, "desc": desc, "icon": icon, "earned": got.get(bid)})
    for cid in [int(k.split(":")[1]) for k in got if k.startswith("curso:")]:
        c = db.one("SELECT title FROM courses WHERE id=?", (cid,))
        if c:
            out.append({"id": f"curso:{cid}", "title": "Curso concluído", "desc": c["title"], "icon": "trophy", "earned": got[f"curso:{cid}"]})
    return out


def xp_info(user, s):
    if not s:
        return {"xp": 0, "nivel": 1, "base": 0, "prox": 150, "pct": 0}
    xp = s["aulas"] * 10 + len(s["cursos_concluidos"]) * 100 + s["mentorias"] * 50 + s["certificados"] * 50 + min(s["sequencia"], 30) * 5
    nivel = 1 + xp // 150
    base = (nivel - 1) * 150
    return {"xp": xp, "nivel": nivel, "base": base, "prox": base + 150, "pct": round(100 * (xp - base) / 150)}


def unseen(user):
    if not user or user["role"] == "admin":
        return []
    meta = {b[0]: b[1] for b in BADGES}
    rows = db.q("SELECT badge FROM badges_earned WHERE user_id=? AND seen=0 ORDER BY earned_at", (user["id"],))
    return [meta.get(r["badge"], "Curso concluído") for r in rows]


def record_attendance(mid, uid):
    db.x("INSERT OR IGNORE INTO attendance(mentoria_id,user_id,joined_at) VALUES (?,?,?)", (mid, uid, core.iso(core.now())))


# ---------------------------------------------------------------- certificado em PDF
def _fmt_data(iso_s):
    d = core.parse(iso_s).astimezone(core.TZ)
    return f"{d.day} de {MESES[d.month - 1]} de {d.year}"


def _fmt_horas(minutes):
    h, m = divmod(max(1, int(minutes)), 60)
    return f"{h}h{m:02d}min" if h and m else (f"{h}h" if h else f"{m} min")


def make_pdf(cert):
    from reportlab.graphics import renderPDF
    from reportlab.graphics.barcode import qr
    from reportlab.graphics.shapes import Drawing
    from reportlab.lib.colors import HexColor
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.utils import simpleSplit
    from reportlab.pdfbase.pdfmetrics import stringWidth
    from reportlab.pdfgen import canvas

    W, H = landscape(A4)
    navy, cyan, ink, grey = HexColor("#0b1b38"), HexColor("#3d93f2"), HexColor("#14223f"), HexColor("#5b6b88")
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(W, H))
    c.setTitle(f"Certificado - {cert['student_name']}")
    c.setAuthor("Rangel Tech")
    c.setFillColor(HexColor("#fbfcff"))
    c.rect(0, 0, W, H, stroke=0, fill=1)
    # moldura e faixa lateral
    c.setFillColor(navy)
    c.rect(0, 0, 26, H, stroke=0, fill=1)
    c.setFillColor(cyan)
    c.rect(26, 0, 4, H, stroke=0, fill=1)
    c.setStrokeColor(HexColor("#c9d8f0"))
    c.setLineWidth(0.8)
    c.rect(52, 28, W - 80, H - 56, stroke=1, fill=0)

    cx = (W + 26) / 2 + 8

    def centered(text, y, font, size, color, spacing=0):
        c.setFillColor(color)
        w = stringWidth(text, font, size) + spacing * max(0, len(text) - 1)
        t = c.beginText(cx - w / 2, y)
        t.setFont(font, size)
        t.setCharSpace(spacing)
        t.textOut(text)
        c.drawText(t)

    centered("RANGEL TECH", H - 84, "Helvetica-Bold", 11, cyan, 4)
    centered("Certificado de conclusão", H - 132, "Helvetica-Bold", 36, navy)
    centered("Certificamos que", H - 172, "Helvetica", 14, grey)
    size = 40
    while stringWidth(cert["student_name"], "Helvetica-Bold", size) > W - 220 and size > 20:
        size -= 2
    centered(cert["student_name"], H - 222, "Helvetica-Bold", size, ink)
    c.setStrokeColor(cyan)
    c.setLineWidth(1.4)
    c.line(cx - 90, H - 240, cx + 90, H - 240)
    centered(f"concluiu {cert['pct']}% do curso", H - 272, "Helvetica", 14, grey)
    y = H - 306
    for ln in simpleSplit(cert["course_title"], "Helvetica-Bold", 24, W - 220)[:3]:
        centered(ln, y, "Helvetica-Bold", 24, navy)
        y -= 30
    centered(f"Carga horária de {_fmt_horas(cert['minutes'])}  ·  {_fmt_data(cert['issued_at'])}", y - 8, "Helvetica", 13, grey)

    # assinatura
    c.setStrokeColor(HexColor("#9fb2d4"))
    c.setLineWidth(0.8)
    c.line(cx - 110, 98, cx + 110, 98)
    centered("Lucas Rangel", 80, "Helvetica-Bold", 14, ink)
    centered("Rangel Tech", 64, "Helvetica", 11, grey)

    # QR e codigo de verificacao
    url = f"{PUBLIC_URL}/verificar/{cert['code']}"
    w = qr.QrCodeWidget(url)
    x0, y0, x1, y1 = w.getBounds()
    size_qr = 74
    d = Drawing(size_qr, size_qr, transform=[size_qr / (x1 - x0), 0, 0, size_qr / (y1 - y0), 0, 0])
    d.add(w)
    renderPDF.draw(d, c, W - 52 - size_qr - 30, 50)
    c.setFillColor(grey)
    c.setFont("Helvetica", 8.5)
    c.drawString(78, 62, f"Código de verificação: {cert['code']}")
    c.drawString(78, 50, f"Confira a autenticidade em {PUBLIC_URL}/verificar/{cert['code']}")
    c.showPage()
    c.save()
    return buf.getvalue()


def issue_certificate(user, course_id):
    """Cria (uma unica vez por aluno e curso) o certificado quando o aluno tem 80% ou mais."""
    c = db.one("SELECT * FROM courses WHERE id=?", (course_id,))
    if not c or not core.has_entitlement(user, "curso", course_id):
        raise HTTPException(404, "Curso não encontrado.")
    tot, done, pct, minutes = cprog(user["id"], course_id)
    if not tot or pct < CERT_MIN_PCT:
        raise HTTPException(403, f"O certificado é liberado a partir de {CERT_MIN_PCT}% do curso.")
    ex = db.one("SELECT * FROM certificates WHERE user_id=? AND course_id=?", (user["id"], course_id))
    if ex:
        return ex
    code = "RT-" + secrets.token_hex(5).upper()
    db.x("INSERT INTO certificates(code,user_id,course_id,student_name,course_title,minutes,pct,issued_at) VALUES (?,?,?,?,?,?,?,?)",
         (code, user["id"], course_id, user["name"], c["title"], minutes, pct, core.iso(core.now())))
    core.audit(user["id"], "certificate_issue", f"course:{course_id}", code)
    return db.one("SELECT * FROM certificates WHERE code=?", (code,))


# ---------------------------------------------------------------- foto de perfil
def photo_path(uid):
    return os.path.join(db.DATA, "photos", f"{int(uid)}.jpg")


def save_photo(uid, data):
    from PIL import Image, ImageOps
    im = Image.open(io.BytesIO(data))
    if im.format not in ("JPEG", "PNG", "WEBP"):
        raise ValueError("formato")
    im = ImageOps.exif_transpose(im).convert("RGB")
    im = ImageOps.fit(im, (320, 320), method=Image.LANCZOS)
    im.save(photo_path(uid), "JPEG", quality=86, optimize=True)
    db.x("UPDATE users SET photo=? WHERE id=?", (str(int(time.time())), uid))


# ---------------------------------------------------------------- TURN (coturn proprio)
def ice_servers(user, base):
    """STUN/TURN da propria VPS. Credencial temporaria (REST API do coturn): usuario = validade:id, senha = HMAC do segredo."""
    secret = os.environ.get("TURN_SECRET", "")
    urls = [u.strip() for u in os.environ.get("TURN_URLS", "").split(",") if u.strip()]
    if not secret or not urls:
        return base
    exp = int(time.time()) + 6 * 3600
    username = f"{exp}:{user['id']}"
    cred = base64.b64encode(hmac.new(secret.encode(), username.encode(), hashlib.sha1).digest()).decode()
    stun = [u for u in urls if u.startswith("stun:")]
    turn = [u for u in urls if u.startswith(("turn:", "turns:"))]
    out = []
    if stun:
        out.append({"urls": stun})
    if turn:
        out.append({"urls": turn, "username": username, "credential": cred})
    return out or base


# ---------------------------------------------------------------- rotas
def setup(app, me, page, form, back, T):
    T.env.globals.update(ICONS=ICONS, cert_min=CERT_MIN_PCT, fmt_data=_fmt_data, fmt_horas=_fmt_horas)

    @app.get("/conquistas", response_class=HTMLResponse)
    def conquistas(request: Request):
        u = me(request)
        s = evaluate(u)
        novas = {r["badge"] for r in db.q("SELECT badge FROM badges_earned WHERE user_id=? AND seen=0", (u["id"],))}
        cat = catalog(u, s)
        db.x("UPDATE badges_earned SET seen=1 WHERE user_id=?", (u["id"],))
        certs = db.q("SELECT * FROM certificates WHERE user_id=? ORDER BY issued_at DESC", (u["id"],))
        elegiveis = []
        if u["role"] != "admin":
            for cid in core.course_ids_for(u):
                co = db.one("SELECT id,title FROM courses WHERE id=? AND published=1", (cid,))
                if not co:
                    continue
                tot, done, pct, _ = cprog(u["id"], cid)
                if tot and not any(x["course_id"] == cid for x in certs):
                    elegiveis.append({**co, "pct": pct, "ok": pct >= CERT_MIN_PCT})
        return page(request, "conquistas.html", u, cat=cat, novas=novas, xp=xp_info(u, s), certs=certs, elegiveis=elegiveis, st=s,
                    total=len(BADGES))

    @app.post("/curso/{cid}/certificado")
    async def certificado_emitir(request: Request, cid: int):
        u = me(request)
        await form(request, u)
        cert = issue_certificate(u, cid)
        evaluate(u)
        return RedirectResponse(f"/certificado/{cert['code']}/pdf", 303)

    @app.get("/certificado/{code}/pdf")
    def certificado_pdf(request: Request, code: str):
        u = me(request)
        cert = db.one("SELECT * FROM certificates WHERE code=?", (code,))
        if not cert or (cert["user_id"] != u["id"] and u["role"] != "admin"):
            raise HTTPException(404, "Certificado não encontrado.")
        return Response(make_pdf(cert), media_type="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="certificado-{code}.pdf"', "Cache-Control": "private, no-store"})

    @app.get("/verificar/{code}", response_class=HTMLResponse)
    def verificar(request: Request, code: str):
        cert = db.one("SELECT * FROM certificates WHERE code=?", (code.strip().upper(),))
        return T.TemplateResponse(request, "verificar.html", {"user": None, "cert": cert, "code": code[:40], "csrf": "", "ok": None, "erro": None})

    # ---- foto
    @app.post("/perfil/foto")
    async def perfil_foto(request: Request):
        u = me(request)
        f = await form(request, u)
        up = f.get("foto")
        if not up or not getattr(up, "filename", ""):
            return back("/perfil", erro="Escolha uma imagem.")
        data = await up.read(MAX_PHOTO + 1)
        if len(data) > MAX_PHOTO:
            return back("/perfil", erro="A imagem passa de 8 MB.")
        try:
            save_photo(u["id"], data)
        except Exception:
            return back("/perfil", erro="Não consegui ler a imagem. Use JPG, PNG ou WebP.")
        core.audit(u["id"], "photo_update", f"user:{u['id']}")
        evaluate(db.one("SELECT * FROM users WHERE id=?", (u["id"],)))
        return back("/perfil", ok="Foto atualizada.")

    @app.post("/perfil/foto/remover")
    async def perfil_foto_remover(request: Request):
        u = me(request)
        await form(request, u)
        try:
            os.remove(photo_path(u["id"]))
        except FileNotFoundError:
            pass
        db.x("UPDATE users SET photo='' WHERE id=?", (u["id"],))
        return back("/perfil", ok="Foto removida.")

    @app.get("/foto/{uid}")
    def foto(request: Request, uid: int):
        u = me(request)
        if uid != u["id"] and u["role"] != "admin":
            raise HTTPException(404, "Não encontrada.")
        p = photo_path(uid)
        if not os.path.isfile(p):
            raise HTTPException(404, "Não encontrada.")
        return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})

    # ---- suporte no WhatsApp (o numero so sai do servidor para quem tem direito)
    @app.get("/suporte")
    def suporte(request: Request):
        u = me(request)
        if not has_support(u):
            raise HTTPException(403, "O suporte no WhatsApp faz parte das mentorias com acompanhamento. Fale com o Lucas para fazer o upgrade.")
        core.audit(u["id"], "support_click", f"user:{u['id']}")
        return RedirectResponse(wa_link(u), 302)

    @app.get("/admin/suporte", response_class=HTMLResponse)
    def admin_suporte(request: Request):
        u = me(request, admin=True)
        alunos = db.q("SELECT id,name,email FROM users WHERE role<>'admin' AND active=1 ORDER BY name")
        for a in alunos:
            a["tem"] = has_support(a)
        return page(request, "admin_suporte.html", u, alunos=alunos, numero=setting("whatsapp_numero", DEFAULT_WA),
                    mensagem=setting("whatsapp_mensagem", DEFAULT_WA_MSG))

    @app.post("/admin/suporte/config")
    async def admin_suporte_config(request: Request):
        u = me(request, admin=True)
        f = await form(request, u)
        num = "".join(ch for ch in (f.get("numero") or "") if ch.isdigit())
        if len(num) < 12:
            return back("/admin/suporte", erro="Informe o número com país e DDD (ex.: 5562999999999).")
        set_setting("whatsapp_numero", num)
        set_setting("whatsapp_mensagem", ((f.get("mensagem") or DEFAULT_WA_MSG).strip())[:300])
        core.audit(u["id"], "support_config", "whatsapp")
        return back("/admin/suporte", ok="Configuração salva.")

    @app.post("/admin/suporte/{uid}")
    async def admin_suporte_toggle(request: Request, uid: int):
        u = me(request, admin=True)
        f = await form(request, u)
        if not db.one("SELECT 1 AS ok FROM users WHERE id=?", (uid,)):
            raise HTTPException(404, "Usuário não encontrado.")
        if f.get("acao") == "liberar":
            core.grant(uid, "suporte", 0, "manual_grant", u["id"], (f.get("motivo") or "suporte pelo painel").strip())
        else:
            for e in db.q("SELECT id FROM entitlements WHERE user_id=? AND resource_type='suporte' AND status='active'", (uid,)):
                core.revoke(e["id"], u["id"], "suporte removido pelo painel")
        return back("/admin/suporte", ok="Atualizado.")
