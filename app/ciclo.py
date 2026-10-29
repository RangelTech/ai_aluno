"""Ciclo de vida: recuperacao de venda (carrinho, Pix, boleto, cartao recusado) e boas-vindas.
O Cakto avisa os eventos pelo webhook (viram 'leads'); uma rotina diaria (DAG no Airflow) chama POST /api/ciclo/executar e este modulo decide
quem recebe qual mensagem, com regras de frequencia, janela de horario e parada. Regras em app/ciclo_regras.json."""
import base64
import hashlib
import hmac
import json
import os
from datetime import timedelta

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from . import core, db

REGRAS_PATH = os.path.join(os.path.dirname(__file__), "ciclo_regras.json")
EVENTOS = {"checkout_abandonment": "carrinho", "pix_gerado": "pix", "boleto_gerado": "boleto", "purchase_refused": "recusado"}
KINDS_VENDA = ("carrinho", "pix", "boleto", "recusado")


def regras():
    return json.load(open(REGRAS_PATH, encoding="utf-8"))


def _key():
    return (os.environ.get("CAKTO_SECRET") or os.environ.get("PRODUCTS_API_TOKEN") or "sem-chave").encode()


def token_sair(email):
    e = base64.urlsafe_b64encode(email.lower().encode()).decode().rstrip("=")
    return e + "." + hmac.new(_key(), e.encode(), hashlib.sha256).hexdigest()[:20]


def email_do_token(tok):
    try:
        e, sig = tok.rsplit(".", 1)
        if not hmac.compare_digest(sig, hmac.new(_key(), e.encode(), hashlib.sha256).hexdigest()[:20]):
            return None
        return base64.urlsafe_b64decode(e + "=" * (-len(e) % 4)).decode()
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------- eventos -> leads
def _contato(d):
    cust = d.get("customer") or {}
    email = str(cust.get("email") or d.get("email") or "").strip().lower()
    return {
        "email": email if core.EMAIL_RE.match(email) else "",
        "name": str(cust.get("name") or d.get("name") or "").strip()[:120],
        "phone": str(cust.get("phone") or cust.get("whatsapp") or d.get("phone") or "").strip()[:30],
        "cpf": (lambda x: x if len(x) in (11, 14) else "")(core.only_digits(cust.get("docNumber") or cust.get("cpf") or d.get("docNumber") or "")),
    }


def _produto_id(d):
    prod = d.get("product") or {}
    return str(prod.get("id") or d.get("product_id") or d.get("productId") or "")


def registrar_evento(event, d):
    """Abandono de carrinho, Pix/boleto gerado ou cartao recusado viram um lead aberto (um por pessoa, produto e tipo a cada 24 h)."""
    kind = EVENTOS.get(event)
    if not kind:
        return "ignorado"
    c = _contato(d)
    pid = _produto_id(d)
    if not (c["email"] or c["phone"]):
        return "sem_contato"
    if not pid or not db.one("SELECT 1 AS ok FROM kiwify_products WHERE kiwify_id=?", (pid,)):
        return "sem_produto"
    desde = core.iso(core.now() - timedelta(hours=24))
    if db.one("SELECT 1 AS ok FROM leads WHERE kind=? AND product_id=? AND status='aberto' AND created_at>=? AND ((?<>'' AND email=?) OR (?<>'' AND phone=?))",
              (kind, pid, desde, c["email"], c["email"], c["phone"], c["phone"])):
        return "duplicado"
    db.x("INSERT INTO leads(kind,email,name,phone,cpf,product_id,order_id,created_at) VALUES (?,?,?,?,?,?,?,?)",
         (kind, c["email"], c["name"], c["phone"], c["cpf"], pid, str(d.get("id") or ""), core.iso(core.now())))
    return "lead_aberto"


def fechar_por_compra(email, cpf):
    """Comprou: encerra a recuperacao de venda desta pessoa (por e-mail ou CPF)."""
    n = 0
    for L in db.q("SELECT id FROM leads WHERE status='aberto' AND kind IN ('carrinho','pix','boleto','recusado') AND ((?<>'' AND email=?) OR (?<>'' AND cpf=?))", (email, email, cpf, cpf)):
        db.x("UPDATE leads SET status='ganho', stop_reason='comprou' WHERE id=?", (L["id"],))
        n += 1
    return n


def agendar_onboarding(user_id, email, name, phone, product_id):
    if db.one("SELECT 1 AS ok FROM leads WHERE kind='onboarding' AND user_id=? AND status='aberto'", (user_id,)):
        return
    db.x("INSERT INTO leads(kind,email,name,phone,product_id,user_id,created_at) VALUES ('onboarding',?,?,?,?,?,?)",
         (email, name, phone, product_id, user_id, core.iso(core.now())))


# ---------------------------------------------------------------- motor
def _motivo_parada(L, age_h, r, passo):
    if L["email"] and db.one("SELECT 1 AS ok FROM optouts WHERE email=?", (L["email"],)):
        return "optout"
    if age_h > r["max_dias"] * 24:
        return "expirou"
    if L["kind"] in KINDS_VENDA:
        if db.one("SELECT 1 AS ok FROM purchases p JOIN users u ON u.id=p.user_id WHERE p.created_at>=? AND ((?<>'' AND u.email=?) OR (?<>'' AND u.cpf=?))",
                  (L["created_at"], L["email"], L["email"], L["cpf"], L["cpf"])):
            return "comprou"
        if not db.one("SELECT 1 AS ok FROM kiwify_products WHERE kiwify_id=? AND pay_url<>''", (L["product_id"],)):
            return "sem_link_do_produto"
    if L["kind"] == "onboarding" and passo and passo.get("se_nunca_entrou"):
        u = db.one("SELECT last_login FROM users WHERE id=?", (L["user_id"],))
        if not u or u["last_login"]:
            return "ja_entrou"
    return ""


def _montar(L, passo):
    """Devolve (assunto, html, texto, link_sair) ou None se nao der para montar (ex.: downsell sem produto abaixo)."""
    from . import extras
    mapa = db.one("SELECT * FROM kiwify_products WHERE kiwify_id=?", (L["product_id"],)) or {}
    nome = (L["name"] or "").split(" ")[0] or "tudo bem"
    link = mapa.get("pay_url", "")
    ctx = {"variante": passo["variante"], "nome": nome, "produto": extras.limpa_nome(mapa.get("label")) or "a mentoria", "link": link,
           "link_login": f"{extras.PUBLIC_URL}/login", "link_esqueci": f"{extras.PUBLIC_URL}/esqueci-senha"}
    if passo.get("usa_downsell"):
        nxt = db.one("SELECT * FROM kiwify_products WHERE kiwify_id=?", (mapa.get("downsell_id", ""),)) if mapa.get("downsell_id") else None
        if not nxt or not nxt["pay_url"]:
            return None
        ctx["oferta_nome"] = extras.limpa_nome(nxt["label"]) or "uma opção mais leve"
        ctx["link"] = nxt["pay_url"]
    sair = f"{extras.PUBLIC_URL}/sair/{token_sair(L['email'])}" if L["email"] else ""
    assunto, html, texto = extras.render_email(passo["modelo"], link_sair=sair, **ctx)
    return assunto, html, texto, sair


def executar(dry=True, agora=None, limite=None):
    from . import extras
    r = regras()
    agora = agora or core.now()
    local = agora.astimezone(core.TZ)
    jan = r["janela_envio"]
    dentro = jan["inicio"] <= local.hour < jan["fim"]
    limite = limite or r.get("limite_por_execucao", 200)
    out = {"dry": dry, "agora": core.iso(agora), "dentro_da_janela": dentro, "enviados": 0, "parados": 0, "aguardando": 0, "sem_canal": 0, "itens": []}
    for L in db.q("SELECT * FROM leads WHERE status='aberto' ORDER BY created_at"):
        cfg = r["tipos"].get(L["kind"])
        if not cfg:
            continue
        passos = cfg["passos"]
        age_h = (agora - core.parse(L["created_at"])).total_seconds() / 3600
        passo = passos[L["etapa"]] if L["etapa"] < len(passos) else None
        motivo = _motivo_parada(L, age_h, r, passo)
        if motivo:
            out["parados"] += 1
            out["itens"].append({"lead": L["id"], "tipo": L["kind"], "acao": "parar", "motivo": motivo})
            if not dry:
                db.x("UPDATE leads SET status='parado', stop_reason=? WHERE id=?", (motivo, L["id"]))
            continue
        if passo is None:
            out["itens"].append({"lead": L["id"], "tipo": L["kind"], "acao": "concluir"})
            if not dry:
                db.x("UPDATE leads SET status='concluido' WHERE id=?", (L["id"],))
            continue
        if age_h < max(passo["apos_horas"], r["min_espera_horas"]):
            out["aguardando"] += 1
            continue
        if L["last_touch_at"] and core.parse(L["last_touch_at"]).astimezone(core.TZ).date() == local.date():
            out["aguardando"] += 1
            out["itens"].append({"lead": L["id"], "tipo": L["kind"], "acao": "aguardar", "motivo": "1_por_dia"})
            continue
        if not dentro:
            out["aguardando"] += 1
            out["itens"].append({"lead": L["id"], "tipo": L["kind"], "acao": "aguardar", "motivo": "fora_da_janela"})
            continue
        if out["enviados"] >= limite:
            break
        msg = _montar(L, passo)
        if msg is None:  # ex.: downsell sem produto abaixo cadastrado: pula o passo
            out["itens"].append({"lead": L["id"], "tipo": L["kind"], "acao": "pular", "motivo": "sem_downsell"})
            if not dry:
                db.x("UPDATE leads SET etapa=etapa+1 WHERE id=?", (L["id"],))
            continue
        assunto, html, texto, sair = msg
        item = {"lead": L["id"], "tipo": L["kind"], "passo": passo["variante"], "email": L["email"], "canais": []}
        if dry:
            item["acao"] = "enviaria"
            out["itens"].append(item)
            out["enviados"] += 1
            continue
        ok_any = False
        for canal in passo.get("canais", ["email"]):
            ok, det = False, ""
            if canal == "email" and L["email"] and extras.smtp_ativo():
                ok, det = extras.send_email(L["email"], assunto, texto, html, list_unsub=sair)
            elif canal == "whatsapp" and L["phone"] and os.environ.get("NOTIFY_WEBHOOK_URL"):
                ok, det = extras.post_webhook({"event": "lifecycle", "kind": L["kind"], "step": passo["variante"], "email": L["email"], "name": L["name"], "phone": L["phone"], "text": texto})
            else:
                continue
            db.x("INSERT INTO touches(lead_id,step,channel,sent_at,ok,detail) VALUES (?,?,?,?,?,?)", (L["id"], L["etapa"], canal, core.iso(agora), 1 if ok else 0, det[:200]))
            item["canais"].append({canal: "ok" if ok else "erro: " + det[:80]})
            ok_any = ok_any or ok
        if ok_any:
            db.x("UPDATE leads SET etapa=etapa+1, last_touch_at=? WHERE id=?", (core.iso(agora), L["id"]))
            item["acao"] = "enviado"
            out["enviados"] += 1
        else:
            item["acao"] = "sem_canal"
            out["sem_canal"] += 1
        out["itens"].append(item)
    if not dry:
        extras.set_setting("ciclo_ultima", json.dumps({k: out[k] for k in ("agora", "enviados", "parados", "aguardando", "sem_canal", "dentro_da_janela")}))
    return out


# ---------------------------------------------------------------- rotas
def setup(app, me, page, form, back, T, extras):
    def _bearer(request):
        tok = os.environ.get("PRODUCTS_API_TOKEN", "")
        if not tok:
            raise HTTPException(503, "api_desativada")
        got = request.headers.get("authorization", "")
        if not got.lower().startswith("bearer ") or not hmac.compare_digest(got[7:].strip(), tok):
            raise HTTPException(401, "token_invalido")

    @app.post("/api/ciclo/executar")
    async def api_ciclo(request: Request):
        """Chamado pela DAG do Airflow. ?dry=1 so simula. (?agora=ISO so com CICLO_TESTE=1, para testes.)"""
        _bearer(request)
        dry = request.query_params.get("dry", "0") in ("1", "true")
        agora = None
        if os.environ.get("CICLO_TESTE") == "1" and request.query_params.get("agora"):
            agora = core.parse(request.query_params["agora"])
        return JSONResponse(executar(dry=dry, agora=agora))

    @app.get("/api/ciclo/status")
    def api_ciclo_status(request: Request):
        _bearer(request)
        return JSONResponse(_resumo())

    def _resumo():
        cont = {f"{r['kind']}:{r['status']}": r["n"] for r in db.q("SELECT kind,status,COUNT(*) n FROM leads GROUP BY kind,status")}
        ult = extras.setting("ciclo_ultima", "")
        return {"leads": cont, "ultima_execucao": json.loads(ult) if ult else None, "optouts": db.one("SELECT COUNT(*) n FROM optouts")["n"]}

    @app.get("/admin/ciclo", response_class=HTMLResponse)
    def admin_ciclo(request: Request):
        u = me(request, admin=True)
        leads = db.q("SELECT l.*, k.label AS plabel FROM leads l LEFT JOIN kiwify_products k ON k.kiwify_id=l.product_id ORDER BY l.id DESC LIMIT 60")
        r = regras()
        for L in leads:
            L["total"] = len(r["tipos"].get(L["kind"], {}).get("passos", []))
        return page(request, "admin_ciclo.html", u, leads=leads, resumo=_resumo(), regras=r, simulacao=None, api_ativa=bool(os.environ.get("PRODUCTS_API_TOKEN")))

    @app.post("/admin/ciclo/simular", response_class=HTMLResponse)
    async def admin_ciclo_simular(request: Request):
        u = me(request, admin=True)
        await form(request, u)
        leads = db.q("SELECT l.*, k.label AS plabel FROM leads l LEFT JOIN kiwify_products k ON k.kiwify_id=l.product_id ORDER BY l.id DESC LIMIT 60")
        r = regras()
        for L in leads:
            L["total"] = len(r["tipos"].get(L["kind"], {}).get("passos", []))
        return page(request, "admin_ciclo.html", u, leads=leads, resumo=_resumo(), regras=r, simulacao=executar(dry=True), api_ativa=bool(os.environ.get("PRODUCTS_API_TOKEN")))

    # ---- sair da lista (link no rodape dos e-mails de ciclo)
    @app.get("/sair/{tok}", response_class=HTMLResponse)
    def sair_get(request: Request, tok: str):
        e = email_do_token(tok)
        return T.TemplateResponse(request, "sair.html", {"user": None, "csrf": "", "ok": None, "erro": None, "valido": bool(e), "feito": False, "tok": tok, "email": e or ""})

    @app.post("/sair/{tok}", response_class=HTMLResponse)
    def sair_post(request: Request, tok: str):
        e = email_do_token(tok)
        if e:
            db.x("INSERT OR IGNORE INTO optouts(email,at) VALUES (?,?)", (e, core.iso(core.now())))
            db.x("UPDATE leads SET status='parado', stop_reason='optout' WHERE status='aberto' AND email=?", (e,))
            core.audit(None, "optout", e)
        return T.TemplateResponse(request, "sair.html", {"user": None, "csrf": "", "ok": None, "erro": None, "valido": bool(e), "feito": bool(e), "tok": tok, "email": e or ""})
