"""Agenda das calls de qualificacao (30 min) para quem saiu do quiz com resultado `cara_a_cara`. Spec SITE_V2, secoes 11, 14.3, 17.6.
Uma agenda so (o Lucas). Janela 7h30-22h (Brasilia) todos os dias, folga de 15 min, antecedencia minima de 2 h, 7 dias a frente.
Lembretes e faltas sao tratados por um laco interno (a cada 10 min); nada depende do Airflow."""
import asyncio
import os
import secrets
from datetime import datetime, timedelta, time as dtime, timezone

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from . import core, db, pg

TZ = core.TZ
JANELA_INI, JANELA_FIM = dtime(7, 30), dtime(22, 0)
DURACAO = timedelta(minutes=30)
FOLGA = timedelta(minutes=15)
ANTECEDENCIA = timedelta(hours=2)
DIAS = 7
ATIVAS = ("marcada", "confirmada")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "https://user.rangeltech.net").rstrip("/")
LOG = []  # ultimas acoes do laco (para o painel)


def agora_teste(request):
    """Em CICLO_TESTE=1 aceita ?agora=ISO para viajar no tempo (testes)."""
    if os.environ.get("CICLO_TESTE") == "1" and request.query_params.get("agora"):
        return core.parse(request.query_params["agora"])
    return core.now()


def fmt(dt):
    d = dt.astimezone(TZ)
    dias = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]
    return f"{dias[d.weekday()]}, {d.strftime('%d/%m')} às {d.strftime('%H:%M')}"


# ---------------------------------------------------------------- ocupacao
def _bloqueado(inicio, fim, bloqueios):
    """Bloqueio comum: sobreposicao direta. Bloqueio semanal: mesmo dia da semana e mesma faixa de hora, em qualquer semana a partir do original."""
    for b in bloqueios:
        bi, bf = b["inicio"], b["fim"]
        if b["repete_semanal"]:
            li, lf = bi.astimezone(TZ), bf.astimezone(TZ)
            d = inicio.astimezone(TZ).date()
            if (d - li.date()).days < 0 or d.weekday() != li.weekday():
                continue
            bi = datetime.combine(d, li.time(), TZ)
            bf = bi + (lf - li)
        if inicio < bf and fim > bi:
            return True
    return False


def livres(agora=None, dias=DIAS):
    """Horarios livres por dia (hora local). Reservas e bloqueios lidos uma vez."""
    agora = agora or core.now()
    minimo = agora + ANTECEDENCIA
    fim_h = agora + timedelta(days=dias + 1)
    reservas = pg.q("SELECT inicio, fim FROM agenda_reservas WHERE estado IN ('marcada','confirmada') AND fim > %s AND inicio < %s", (agora - timedelta(hours=1), fim_h))
    bloqueios = pg.q("SELECT inicio, fim, repete_semanal FROM agenda_bloqueios WHERE repete_semanal OR fim > %s", (agora - timedelta(hours=1),))
    out = []
    hoje = agora.astimezone(TZ).date()
    for i in range(dias):
        d = hoje + timedelta(days=i)
        t = datetime.combine(d, JANELA_INI, TZ)
        limite = datetime.combine(d, JANELA_FIM, TZ)
        horarios = []
        while t + DURACAO <= limite:
            fim = t + DURACAO
            ok = t >= minimo
            if ok:
                for r in reservas:
                    if t < r["fim"] + FOLGA and fim > r["inicio"] - FOLGA:
                        ok = False
                        break
            if ok and _bloqueado(t, fim, bloqueios):
                ok = False
            if ok:
                horarios.append(t.strftime("%H:%M"))
            t += DURACAO
        out.append({"data": d.isoformat(), "rotulo": d.strftime("%d/%m"), "dia": ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"][d.weekday()], "horarios": horarios})
    return out


def _parse_inicio(s):
    try:
        dt = datetime.strptime(s, "%Y-%m-%dT%H:%M").replace(tzinfo=TZ)
    except (TypeError, ValueError):
        raise HTTPException(400, "inicio_invalido")
    if dt.minute % 30 or dt.second:
        raise HTTPException(400, "inicio_invalido")
    return dt


def _lead_ok(token):
    L = pg.one("SELECT * FROM quiz_leads WHERE token=%s", (token or "",))
    if not L or L["resultado"] != "cara_a_cara":
        raise HTTPException(403, "sem_direito_a_agenda")
    return L


def reserva_ativa(token):
    return pg.one("SELECT * FROM agenda_reservas WHERE token=%s AND estado IN ('marcada','confirmada') ORDER BY inicio LIMIT 1", (token,))


def reservar(L, inicio, agora=None):
    """Transacional: trava consultiva + indice unico parcial em `inicio`. Devolve a reserva ou levanta 409."""
    agora = agora or core.now()
    fim = inicio + DURACAO
    dia_local = inicio.astimezone(TZ)
    if not (JANELA_INI <= dia_local.time() and (dia_local + DURACAO).time() <= JANELA_FIM) or inicio < agora + ANTECEDENCIA or inicio > agora + timedelta(days=DIAS + 1):
        raise HTTPException(409, "horario_indisponivel")
    with pg.conn() as c:
        c.execute("SELECT pg_advisory_xact_lock(hashtext('agenda_reservas'))")
        if c.execute("SELECT 1 FROM agenda_reservas WHERE token=%s AND estado IN ('marcada','confirmada')", (L["token"],)).fetchone():
            raise HTTPException(409, "ja_tem_reserva")
        choque = c.execute("SELECT 1 FROM agenda_reservas WHERE estado IN ('marcada','confirmada') AND inicio < %s AND fim > %s", (fim + FOLGA, inicio - FOLGA)).fetchone()
        blq = c.execute("SELECT inicio, fim, repete_semanal FROM agenda_bloqueios WHERE repete_semanal OR fim > %s", (inicio - timedelta(days=1),)).fetchall()
        if choque or _bloqueado(inicio, fim, blq):
            raise HTTPException(409, "horario_indisponivel")
        codigo = secrets.token_urlsafe(6).replace("-", "a").replace("_", "b")[:8].upper()
        try:
            r = c.execute("INSERT INTO agenda_reservas(codigo,token,inicio,fim,cancel_token) VALUES (%s,%s,%s,%s,%s) RETURNING *",
                          (codigo, L["token"], inicio, fim, secrets.token_urlsafe(16))).fetchone()
        except Exception as e:  # noqa: BLE001  (indice unico: dois pedidos no mesmo instante)
            if "ux_reserva_inicio" in str(e) or "unique" in str(e).lower():
                raise HTTPException(409, "horario_indisponivel")
            raise
    pg.x("UPDATE quiz_leads SET status='agendado', atualizado_em=now() WHERE token=%s", (L["token"],))
    if L.get("lead_id"):
        db.x("UPDATE leads SET status='ganho', stop_reason='agendou' WHERE id=? AND status IN ('aberto','parado')", (L["lead_id"],))
    db.x("UPDATE leads SET status='parado', stop_reason='agendou' WHERE kind='faltou' AND order_id=? AND status='aberto'", (L["token"],))
    core.audit(None, "agenda_reserva", r["codigo"], f"{L['token']} {core.iso(inicio)}")
    return r


# ---------------------------------------------------------------- e-mails
def _ctx(r, L, **extra):
    from . import funil
    return {"nome": (L["nome"] or "").split(" ")[0] or "tudo bem", "quando": fmt(r["inicio"]), "link_chamada": r["link_chamada"],
            "link_reserva": f"{PUBLIC_URL}/agenda/{r['codigo']}?k={r['cancel_token']}", "link_agendar": f"{PUBLIC_URL}/agendar?t={L['token']}",
            "whatsapp": L["whatsapp"], "email": L["email"], "resumo": funil._linhas_resumo(L), "link_admin": f"{PUBLIC_URL}/admin/agenda", **extra}


def enviar(modelo, para, r, L, **extra):
    from . import extras
    if not para or not extras.smtp_ativo():
        return False, "smtp_desligado"
    assunto, html, texto = extras.render_email(modelo, **_ctx(r, L, **extra))
    ok, det = extras.send_email(para, assunto, texto, html)
    core.audit(None, f"agenda_email_{modelo}" + ("" if ok else "_falhou"), r["codigo"], det[:200])
    return ok, det


def confirmar_por_email(r, L):
    enviar("agenda_confirmacao", L["email"], r, L, variante="nova")
    enviar("agenda_aviso", os.environ.get("MASTER_EMAIL", ""), r, L, variante="reserva")


# ---------------------------------------------------------------- laco de lembretes e faltas
def tick(agora=None, dry=False):
    """Executado a cada 10 min (e por POST /api/agenda/rodar). Devolve o que fez."""
    if not pg.ativo():
        return {"erro": "banco_indisponivel"}
    agora = agora or core.now()
    out = {"agora": core.iso(agora), "lembrete_24h": 0, "lembrete_1h": 0, "faltou": 0, "dry": dry}
    rows = pg.q("SELECT r.*, q.nome, q.email, q.whatsapp, q.lead_id, q.situacao, q.objetivo, q.problema, q.tentou, q.custo_mes, q.prazo, q.formato, q.compromisso, "
                "q.empresa_porte, q.empresa_decisor, q.token AS qtoken FROM agenda_reservas r JOIN quiz_leads q ON q.token=r.token "
                "WHERE r.estado IN ('marcada','confirmada') AND r.inicio < %s", (agora + timedelta(hours=25),))
    for r in rows:
        L = {**r, "token": r["qtoken"]}
        falta = r["inicio"] - agora
        if r["fim"] + timedelta(hours=2) < agora:
            out["faltou"] += 1
            if not dry:
                marcar_falta(r, L, automatico=True)
            continue
        if falta <= timedelta(hours=1) and not r["lembrete_1h_em"] and falta > timedelta(0):
            out["lembrete_1h"] += 1
            if not dry:
                enviar("agenda_lembrete", L["email"], r, L, variante="1h")
                pg.x("UPDATE agenda_reservas SET lembrete_1h_em=%s WHERE id=%s", (agora, r["id"]))
        elif falta <= timedelta(hours=24) and not r["lembrete_24h_em"] and falta > timedelta(hours=1):
            out["lembrete_24h"] += 1
            if not dry:
                enviar("agenda_lembrete", L["email"], r, L, variante="24h")
                pg.x("UPDATE agenda_reservas SET lembrete_24h_em=%s WHERE id=%s", (agora, r["id"]))
    LOG.append(out)
    del LOG[:-20]
    return out


def marcar_falta(r, L, automatico=False):
    pg.x("UPDATE agenda_reservas SET estado='faltou', atualizado_em=now() WHERE id=%s AND estado IN ('marcada','confirmada')", (r["id"],))
    pg.x("UPDATE quiz_leads SET status='faltou', atualizado_em=now() WHERE token=%s", (L["token"],))
    # volta para a recuperacao: lead `faltou` no motor de ciclo (2 passos: 2 h e 48 h), order_id guarda o token para o link de remarcar
    if not db.one("SELECT 1 AS ok FROM leads WHERE kind='faltou' AND order_id=? AND status='aberto'", (L["token"],)):
        pid = (db.one("SELECT kiwify_id FROM kiwify_products WHERE plan=1 LIMIT 1") or {}).get("kiwify_id", "")
        db.x("INSERT INTO leads(kind,email,name,phone,product_id,order_id,created_at) VALUES ('faltou',?,?,?,?,?,?)",
             (L["email"], L["nome"], L["whatsapp"], pid, L["token"], core.iso(core.now())))
    core.audit(None, "agenda_faltou", r["codigo"], "automatico" if automatico else "admin")


async def laco():
    await asyncio.sleep(20)
    while True:
        try:
            await asyncio.to_thread(tick)
        except Exception as e:  # noqa: BLE001
            LOG.append({"erro": str(e)[:200]})
        await asyncio.sleep(600)


# ---------------------------------------------------------------- iCalendar
def ics():
    def dt(d):
        return d.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    linhas = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Rangel Tech//Agenda//PT", "X-WR-CALNAME:Calls de qualificação"]
    for r in pg.q("SELECT r.*, q.nome FROM agenda_reservas r JOIN quiz_leads q ON q.token=r.token WHERE r.estado IN ('marcada','confirmada','compareceu') AND r.inicio > now() - interval '30 days'"):
        linhas += ["BEGIN:VEVENT", f"UID:reserva-{r['codigo']}@rangeltech.net", f"DTSTART:{dt(r['inicio'])}", f"DTEND:{dt(r['fim'])}",
                   f"SUMMARY:Call {r['nome']}", f"DESCRIPTION:{PUBLIC_URL}/admin/agenda", "END:VEVENT"]
    for b in pg.q("SELECT * FROM agenda_bloqueios WHERE repete_semanal OR fim > now() - interval '7 days'"):
        linhas += ["BEGIN:VEVENT", f"UID:bloqueio-{b['id']}@rangeltech.net", f"DTSTART:{dt(b['inicio'])}", f"DTEND:{dt(b['fim'])}",
                   f"SUMMARY:Bloqueio {b['motivo']}".rstrip()] + (["RRULE:FREQ=WEEKLY"] if b["repete_semanal"] else []) + ["END:VEVENT"]
    linhas.append("END:VCALENDAR")
    return "\r\n".join(linhas) + "\r\n"


# ---------------------------------------------------------------- rotas
def setup(app, me, page, form, back, T, extras):
    T.env.globals.update(agenda_fmt=fmt)

    def _reserva(codigo, k=None, exigir_k=True):
        r = pg.one("SELECT r.*, q.nome, q.email, q.whatsapp, q.token AS qtoken FROM agenda_reservas r JOIN quiz_leads q ON q.token=r.token WHERE r.codigo=%s", (codigo.upper(),))
        if not r or (exigir_k and (not k or not secrets.compare_digest(k, r["cancel_token"]))):
            raise HTTPException(404, "Reserva não encontrada.")
        return r

    def _lead_de(r):
        return pg.one("SELECT * FROM quiz_leads WHERE token=%s", (r["qtoken"],))

    @app.get("/api/agenda/horarios")
    def api_horarios(request: Request):
        pg.exigir()
        L = _lead_ok(request.query_params.get("t"))
        ativa = reserva_ativa(L["token"])
        return {"fuso": "America/Sao_Paulo", "dias": livres(agora_teste(request)), "reserva": ({"codigo": ativa["codigo"], "quando": fmt(ativa["inicio"])} if ativa else None)}

    @app.post("/api/agenda/reservar")
    async def api_reservar(request: Request):
        pg.exigir()
        try:
            d = await request.json()
        except Exception:  # noqa: BLE001
            raise HTTPException(400, "json_invalido")
        L = _lead_ok(d.get("t"))
        r = reservar(L, _parse_inicio(str(d.get("inicio") or "")), agora_teste(request))
        confirmar_por_email(r, L)
        return JSONResponse({"ok": True, "codigo": r["codigo"], "quando": fmt(r["inicio"]), "url_cancelar": f"{PUBLIC_URL}/agenda/{r['codigo']}?k={r['cancel_token']}"}, status_code=201)

    @app.post("/api/agenda/rodar")
    async def api_rodar(request: Request):
        """Forca uma rodada do laco (lembretes e faltas). Bearer PRODUCTS_API_TOKEN; ?dry=1 so conta."""
        import hmac
        tok = os.environ.get("PRODUCTS_API_TOKEN", "")
        got = request.headers.get("authorization", "")
        if not tok or not got.lower().startswith("bearer ") or not hmac.compare_digest(got[7:].strip(), tok):
            raise HTTPException(401, "token_invalido")
        return tick(agora_teste(request), dry=request.query_params.get("dry") in ("1", "true"))

    # ---- paginas publicas
    @app.get("/agendar", response_class=HTMLResponse)
    def agendar(request: Request):
        pg.exigir()
        t = request.query_params.get("t", "")
        L = pg.one("SELECT * FROM quiz_leads WHERE token=%s", (t,)) if t else None
        if not L or L["resultado"] != "cara_a_cara":
            return T.TemplateResponse(request, "agendar.html", {"user": None, "csrf": "", "ok": None, "erro": "Este link não dá acesso à agenda. Volte ao site e responda às perguntas.", "lead": None, "reserva": None, "token": t}, status_code=403)
        ativa = reserva_ativa(L["token"])
        return T.TemplateResponse(request, "agendar.html", {"user": None, "csrf": "", "ok": None, "erro": None, "lead": L, "reserva": ativa, "token": t, "nome": (L["nome"] or "").split(" ")[0]})

    @app.get("/agenda/{codigo}", response_class=HTMLResponse)
    def ver_reserva(request: Request, codigo: str):
        pg.exigir()
        r = _reserva(codigo, request.query_params.get("k"))
        return T.TemplateResponse(request, "agenda_reserva.html", {"user": None, "csrf": "", "ok": request.query_params.get("ok"), "erro": request.query_params.get("erro"), "r": r, "k": request.query_params.get("k")})

    @app.post("/agenda/{codigo}/cancelar")
    async def cancelar_reserva(request: Request, codigo: str):
        pg.exigir()
        f = await request.form()
        r = _reserva(codigo, f.get("k"))
        if r["estado"] in ATIVAS:
            pg.x("UPDATE agenda_reservas SET estado='cancelada', atualizado_em=now() WHERE id=%s", (r["id"],))
            pg.x("UPDATE quiz_leads SET status='concluido', atualizado_em=now() WHERE token=%s", (r["qtoken"],))
            L = _lead_de(r)
            if L and L.get("lead_id"):  # volta a recuperar: a pessoa pode remarcar pelo link do e-mail
                db.x("UPDATE leads SET status='aberto', stop_reason='' WHERE id=? AND status<>'aberto'", (L["lead_id"],))
            core.audit(None, "agenda_cancelada", r["codigo"], "pela pessoa")
        return back(f"/agenda/{codigo}?k={f.get('k')}", ok="Reserva cancelada. Se quiser, marque outro horário.")

    # ---- admin
    @app.get("/admin/agenda", response_class=HTMLResponse)
    def admin_agenda(request: Request):
        u = me(request, admin=True)
        if not pg.ativo():
            return page(request, "admin_agenda.html", u, banco=False, dias=[], bloqueios=[], ics_url="", log=LOG)
        agora = core.now()
        hoje = agora.astimezone(TZ).date()
        reservas = pg.q("SELECT r.*, q.nome, q.email, q.whatsapp, q.situacao, q.objetivo, q.problema, q.prazo, q.custo_mes FROM agenda_reservas r JOIN quiz_leads q ON q.token=r.token "
                        "WHERE r.inicio >= %s AND r.inicio < %s ORDER BY r.inicio", (datetime.combine(hoje, dtime(0, 0), TZ), datetime.combine(hoje + timedelta(days=14), dtime(0, 0), TZ)))
        bloqueios = pg.q("SELECT * FROM agenda_bloqueios ORDER BY repete_semanal DESC, inicio")
        dias = []
        for i in range(14):
            d = hoje + timedelta(days=i)
            dias.append({"data": d, "rotulo": d.strftime("%d/%m"), "dia": ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"][d.weekday()],
                         "reservas": [r for r in reservas if r["inicio"].astimezone(TZ).date() == d],
                         "bloqueios": [b for b in bloqueios if _bloqueado(datetime.combine(d, JANELA_INI, TZ), datetime.combine(d, JANELA_FIM, TZ), [b])]})
        k = extras.setting("agenda_ics_key", "")
        if not k:
            k = secrets.token_urlsafe(18)
            extras.set_setting("agenda_ics_key", k)
        return page(request, "admin_agenda.html", u, banco=True, dias=dias, bloqueios=bloqueios, ics_url=f"{PUBLIC_URL}/admin/agenda.ics?k={k}", log=LOG[-5:], tz=TZ)

    @app.get("/admin/agenda.ics")
    def admin_ics(request: Request):
        k = extras.setting("agenda_ics_key", "")
        if not k or not secrets.compare_digest(request.query_params.get("k", ""), k):
            raise HTTPException(404, "nao_encontrado")
        pg.exigir()
        return PlainTextResponse(ics(), media_type="text/calendar; charset=utf-8", headers={"Content-Disposition": "inline; filename=agenda.ics"})

    @app.post("/admin/agenda/bloqueio")
    async def admin_bloqueio(request: Request):
        u = me(request, admin=True)
        f = await form(request, u)
        pg.exigir()
        try:
            ini = datetime.strptime(f.get("inicio", ""), "%Y-%m-%dT%H:%M").replace(tzinfo=TZ)
            fim = datetime.strptime(f.get("fim", ""), "%Y-%m-%dT%H:%M").replace(tzinfo=TZ)
        except ValueError:
            return back("/admin/agenda", erro="Informe início e fim.")
        if fim <= ini or fim - ini > timedelta(days=7):
            return back("/admin/agenda", erro="O fim precisa ser depois do início (máximo 7 dias).")
        pg.x("INSERT INTO agenda_bloqueios(inicio,fim,repete_semanal,motivo) VALUES (%s,%s,%s,%s)", (ini, fim, bool(f.get("repete")), (f.get("motivo") or "").strip()[:80]))
        core.audit(u["id"], "agenda_bloqueio", core.iso(ini), "semanal" if f.get("repete") else "")
        return back("/admin/agenda", ok="Bloqueio criado.")

    @app.post("/admin/agenda/bloqueio/{bid}/remover")
    async def admin_bloqueio_remover(request: Request, bid: int):
        u = me(request, admin=True)
        await form(request, u)
        pg.x("DELETE FROM agenda_bloqueios WHERE id=%s", (bid,))
        return back("/admin/agenda", ok="Bloqueio removido.")

    @app.post("/admin/agenda/reserva/{codigo}/{acao}")
    async def admin_reserva(request: Request, codigo: str, acao: str):
        u = me(request, admin=True)
        f = await form(request, u)
        r = _reserva(codigo, exigir_k=False)
        L = _lead_de(r)
        if acao == "compareceu":
            pg.x("UPDATE agenda_reservas SET estado='compareceu', atualizado_em=now() WHERE id=%s", (r["id"],))
            pg.x("UPDATE quiz_leads SET status='compareceu', atualizado_em=now() WHERE token=%s", (r["qtoken"],))
        elif acao == "faltou":
            marcar_falta(r, L)
        elif acao == "cancelar":
            pg.x("UPDATE agenda_reservas SET estado='cancelada', atualizado_em=now() WHERE id=%s", (r["id"],))
            pg.x("UPDATE quiz_leads SET status='concluido', atualizado_em=now() WHERE token=%s", (r["qtoken"],))
        elif acao == "link":
            link = (f.get("link_chamada") or "").strip()[:300]
            pg.x("UPDATE agenda_reservas SET link_chamada=%s, atualizado_em=now() WHERE id=%s", (link, r["id"]))
            if link:
                enviar("agenda_confirmacao", L["email"], {**r, "link_chamada": link}, L, variante="link")
        else:
            raise HTTPException(404, "acao")
        core.audit(u["id"], f"agenda_{acao}", codigo)
        return back("/admin/agenda", ok="Reserva atualizada.")
