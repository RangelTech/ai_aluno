"""Funil de qualificacao (quiz do site ai.rangeltech.net). Contrato: spec SITE_V2, secao 17.3.
O lead nasce na tela 4 (nome, WhatsApp, e-mail) e tambem abre um lead `quiz` no motor de ciclo (SQLite) para a recuperacao."""
import json
import os
import secrets
import time
from urllib.parse import quote

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from . import ciclo, core, db, pg

SITE_URL = os.environ.get("SITE_URL", "https://ai.rangeltech.net").rstrip("/")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "https://user.rangeltech.net").rstrip("/")
WHATSAPP = os.environ.get("SUPPORT_WHATSAPP", "5562985613482")
CHECKOUT_1K_PADRAO = "https://pay.cakto.com.br/qddg2xq"

SITUACOES = ("empresa", "empregado", "desempregado", "autonomo")
OBJETIVOS = ("saas", "renda", "tempo", "carreira", "aprender", "outro", "atendimento", "vendas", "sistema", "dados", "custo", "empresa")
PRAZOS = ("semana", "30dias", "3meses", "pesquisando")
FORMATOS = ("whatsapp", "cara_a_cara")
CUSTOS = ("ate500", "500a2000", "2000a10000", "mais10000", "naosei", "ate3000", "3000a6000", "6000a12000", "mais12000")
COMPROMISSOS = ("sim", "nao", "depende")
QUENTE_PRAZOS = ("semana", "30dias")

ROTULOS = {
    "situacao": {"empresa": "tem uma empresa", "empregado": "está empregado", "desempregado": "está desempregado", "autonomo": "é autônomo"},
    "objetivo": {"saas": "criar um software ou SaaS", "renda": "ganhar renda online", "tempo": "ganhar tempo com automação", "carreira": "mudar de carreira para IA",
                 "empresa": "resolver um problema da empresa", "aprender": "aprender IA do zero", "outro": "outro objetivo",
                 "atendimento": "resolver o atendimento", "vendas": "vender mais com menos gente", "sistema": "trocar o sistema velho", "dados": "organizar os dados da empresa", "custo": "cortar custo com tarefa que a IA faz melhor"},
    "prazo": {"semana": "esta semana", "30dias": "em até 30 dias", "3meses": "em até 3 meses", "pesquisando": "só pesquisando"},
    "custo_mes": {"ate500": "até R$ 500 por mês", "500a2000": "de R$ 500 a R$ 2.000 por mês", "2000a10000": "de R$ 2.000 a R$ 10.000 por mês", "mais10000": "mais de R$ 10.000 por mês", "naosei": "não sabe",
                  "ate3000": "pessoa/processo de até R$ 3.000 por mês", "3000a6000": "pessoa/processo de R$ 3.000 a R$ 6.000 por mês", "6000a12000": "pessoa/processo de R$ 6.000 a R$ 12.000 por mês", "mais12000": "pessoa/processo de mais de R$ 12.000 por mês"},
    "porte": {"so_eu": "só uma pessoa", "2a10": "2 a 10 pessoas", "11a50": "11 a 50 pessoas", "mais50": "mais de 50 pessoas"},
    "decisor": {"sim": "quem decide é a própria pessoa", "parte": "decide junto com outras pessoas", "nao": "não é quem decide"},
    "formato": {"whatsapp": "pelo WhatsApp", "cara_a_cara": "cara a cara"},
}
PROXIMA_TELA = ("problema", 5), ("tentou", 6), ("custo_mes", 7), ("prazo", 8), ("formato", 9)


def origens_permitidas():
    o = [SITE_URL]
    if os.environ.get("CICLO_TESTE") == "1":
        o += ["http://localhost:4321", "http://127.0.0.1:4321"]
    return o


# ---------------------------------------------------------------- limite de taxa (por IP, memoria do processo)
_hits = {}


def limitar(ip, limite=40, janela=600):
    n, t0 = _hits.get(ip, (0, time.time()))
    if time.time() - t0 > janela:
        n, t0 = 0, time.time()
    n += 1
    _hits[ip] = (n, t0)
    if len(_hits) > 5000:
        _hits.clear()
    if n > limite:
        raise HTTPException(429, "muitas_tentativas")


def _ip(request):
    return request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else "?")


async def _json(request):
    try:
        d = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "json_invalido")
    if not isinstance(d, dict):
        raise HTTPException(400, "json_invalido")
    if str(d.get("site") or "").strip():  # campo armadilha: humano nunca preenche
        raise HTTPException(400, "invalido")
    return d


def _txt(d, k, n=400):
    return str(d.get(k) or "").strip()[:n]


def _opcao(d, k, validas):
    v = _txt(d, k, 40)
    if v and v not in validas:
        raise HTTPException(400, f"{k}_invalido")
    return v


# ---------------------------------------------------------------- produto do 1k
def checkout_1k():
    r = db.one("SELECT pay_url, kiwify_id FROM kiwify_products WHERE plan=1 AND pay_url<>'' ORDER BY hidden, kiwify_id LIMIT 1")
    if r:
        return r["pay_url"], r["kiwify_id"]
    from . import extras
    return extras.setting("checkout_1k", CHECKOUT_1K_PADRAO), (db.one("SELECT kiwify_id FROM kiwify_products WHERE plan=1 LIMIT 1") or {}).get("kiwify_id", "")


# ---------------------------------------------------------------- resultado
def resumo(L):
    return {"nome": L["nome"], "problema": L["problema"], "custo_mes": L["custo_mes"], "custo_mes_rotulo": ROTULOS["custo_mes"].get(L["custo_mes"], ""),
            "situacao": L["situacao"], "situacao_rotulo": ROTULOS["situacao"].get(L["situacao"], ""), "objetivo_rotulo": ROTULOS["objetivo"].get(L["objetivo"], "")}


def texto_whatsapp(L):
    partes = [f"Oi, Lucas! Sou {L['nome'] or 'eu'} e vim pelo site."]
    if L["situacao"]:
        partes.append(f"Eu {ROTULOS['situacao'].get(L['situacao'], L['situacao'])}" + (f" e quero {ROTULOS['objetivo'][L['objetivo']]}." if L["objetivo"] in ROTULOS["objetivo"] else "."))
    if L["problema"]:
        partes.append(f"Meu problema: {L['problema'][:300]}")
    if L["empresa_porte"]:
        partes.append(f"Empresa com {ROTULOS['porte'].get(L['empresa_porte'], L['empresa_porte'])}." + (f" {ROTULOS['decisor'].get(L['empresa_decisor'], L['empresa_decisor']).capitalize()}." if L["empresa_decisor"] else ""))
    if L["prazo"] in ROTULOS["prazo"]:
        partes.append(f"Quero resolver {ROTULOS['prazo'][L['prazo']]}.")
    return " ".join(partes)


def calcular(L):
    """Regras da spec (10.5, 15.5, 17.3). Devolve (resultado, quente, destino_tipo, destino_url)."""
    quente = L["prazo"] in QUENTE_PRAZOS and bool(L["problema"])
    if L["situacao"] == "empresa" or L["objetivo"] == "empresa":
        return "empresa", quente, "whatsapp", f"https://wa.me/{WHATSAPP}?text={quote(texto_whatsapp(L))}"
    if L["prazo"] == "pesquisando":
        return "frio", False, "obrigado", ""
    if L["formato"] == "cara_a_cara" and L["prazo"] in QUENTE_PRAZOS:
        return "cara_a_cara", quente, "agenda", f"{PUBLIC_URL}/agendar?t={L['token']}"
    return "whatsapp", quente, "checkout", checkout_1k()[0]


def proxima_tela(L):
    for campo, n in PROXIMA_TELA:
        if not L[campo]:
            return n
    return 9


def fechar_lead_ciclo(L, status, motivo):
    if L.get("lead_id"):
        db.x("UPDATE leads SET status=?, stop_reason=? WHERE id=? AND status='aberto'", (status, motivo, L["lead_id"]))


def carregar(token):
    pg.exigir()
    if not token or len(token) > 64:
        raise HTTPException(404, "nao_encontrado")
    L = pg.one("SELECT * FROM quiz_leads WHERE token=%s", (token,))
    if not L:
        raise HTTPException(404, "nao_encontrado")
    return L


def publico(L):
    """Respostas que o site pode ver (sem ip, sem lead_id)."""
    campos = ("token", "situacao", "objetivo", "nome", "whatsapp", "email", "problema", "tentou", "custo_mes", "prazo", "formato", "compromisso",
              "empresa_porte", "empresa_decisor", "etapa", "resultado", "destino_tipo", "destino_url", "status")
    return {k: L[k] for k in campos}


# ---------------------------------------------------------------- rotas
def setup(app, me, page, form, back, T, extras):
    from starlette.middleware.cors import CORSMiddleware
    app.add_middleware(CORSMiddleware, allow_origins=origens_permitidas(), allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
                       allow_headers=["Content-Type"], max_age=600)

    @app.post("/api/quiz")
    async def quiz_criar(request: Request):
        pg.exigir()
        limitar(_ip(request), limite=20)
        d = await _json(request)
        nome = _txt(d, "nome", 120)
        email = _txt(d, "email", 160).lower()
        fone = ciclo.fone_wa(_txt(d, "whatsapp", 30))
        if not nome or len(nome) < 2:
            raise HTTPException(400, "nome_invalido")
        if not core.EMAIL_RE.match(email):
            raise HTTPException(400, "email_invalido")
        if not fone:
            raise HTTPException(400, "whatsapp_invalido")
        situacao = _opcao(d, "situacao", SITUACOES)
        objetivo = _opcao(d, "objetivo", OBJETIVOS)
        utm = d.get("origem") if isinstance(d.get("origem"), dict) else {}
        utm = {str(k)[:40]: str(v)[:120] for k, v in list(utm.items())[:12]}
        token = secrets.token_urlsafe(18)
        pay_url, pid = checkout_1k()
        lead_id = db.x("INSERT INTO leads(kind,email,name,phone,product_id,order_id,created_at) VALUES ('quiz',?,?,?,?,?,?)",
                       (email, nome, fone, pid, token, core.iso(core.now())))
        pg.x("INSERT INTO quiz_leads(token,situacao,objetivo,nome,whatsapp,email,utm,lead_id,ip) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
             (token, situacao, objetivo, nome, fone, email, json.dumps(utm), lead_id, _ip(request)[:60]))
        core.audit(None, "quiz_inicio", token, f"{situacao} {objetivo}")
        return JSONResponse({"token": token}, status_code=201)

    @app.patch("/api/quiz/{token}")
    async def quiz_atualizar(request: Request, token: str):
        limitar(_ip(request))
        L = carregar(token)
        if L["status"] not in ("aberto", "concluido"):
            raise HTTPException(409, "quiz_fechado")
        d = await _json(request)
        novo = {
            "problema": _txt(d, "problema", 600) or L["problema"], "tentou": _txt(d, "tentou", 600) or L["tentou"],
            "custo_mes": _opcao(d, "custo_mes", CUSTOS) or L["custo_mes"], "prazo": _opcao(d, "prazo", PRAZOS) or L["prazo"],
            "formato": _opcao(d, "formato", FORMATOS) or L["formato"], "compromisso": _opcao(d, "compromisso", COMPROMISSOS) or L["compromisso"],
            "situacao": _opcao(d, "situacao", SITUACOES) or L["situacao"], "objetivo": _opcao(d, "objetivo", OBJETIVOS) or L["objetivo"],
        }
        emp = d.get("empresa") if isinstance(d.get("empresa"), dict) else {}
        novo["empresa_porte"] = str(emp.get("porte") or "").strip()[:60] or L["empresa_porte"]
        novo["empresa_decisor"] = str(emp.get("decisor") or "").strip()[:60] or L["empresa_decisor"]
        L2 = {**L, **novo}
        etapa = proxima_tela(L2) - 1 if proxima_tela(L2) < 9 or not L2["formato"] else 9
        pg.x("UPDATE quiz_leads SET problema=%s,tentou=%s,custo_mes=%s,prazo=%s,formato=%s,compromisso=%s,situacao=%s,objetivo=%s,"
             "empresa_porte=%s,empresa_decisor=%s,etapa=%s,atualizado_em=now() WHERE token=%s",
             (novo["problema"], novo["tentou"], novo["custo_mes"], novo["prazo"], novo["formato"], novo["compromisso"], novo["situacao"], novo["objetivo"],
              novo["empresa_porte"], novo["empresa_decisor"], max(etapa, L["etapa"]), token))
        return {"ok": True, "etapa": max(etapa, L["etapa"])}

    @app.post("/api/quiz/{token}/concluir")
    async def quiz_concluir(request: Request, token: str):
        limitar(_ip(request))
        L = carregar(token)
        if L["status"] in ("agendado", "faltou") and L["destino_tipo"]:
            return _saida(L)
        if not L["prazo"] and L["situacao"] != "empresa":
            raise HTTPException(400, "prazo_obrigatorio")
        resultado, quente, tipo, url = calcular(L)
        pg.x("UPDATE quiz_leads SET resultado=%s,quente=%s,destino_tipo=%s,destino_url=%s,status='concluido',etapa=9,atualizado_em=now() WHERE token=%s",
             (resultado, quente, tipo, url, token))
        L = {**L, "resultado": resultado, "quente": quente, "destino_tipo": tipo, "destino_url": url}
        if tipo in ("agenda", "checkout", "whatsapp"):
            fechar_lead_ciclo(L, "parado", f"quiz_concluido:{tipo}")
        core.audit(None, "quiz_fim", token, f"{resultado} -> {tipo}" + (" quente" if quente else ""))
        if quente and tipo == "whatsapp" and extras.smtp_ativo() and os.environ.get("MASTER_EMAIL"):
            # empresa quente: avisa o Lucas na hora (o lead vai para o WhatsApp dele)
            assunto, html, texto = extras.render_email("agenda_aviso", variante="empresa", nome=L["nome"], whatsapp=L["whatsapp"], email=L["email"],
                                                       resumo=_linhas_resumo(L), quando="", link_admin=f"{PUBLIC_URL}/admin/funil")
            extras.send_email(os.environ["MASTER_EMAIL"], assunto, texto, html)
        return _saida(L)

    def _saida(L):
        return {"resultado": L["resultado"], "quente": bool(L["quente"]), "destino": {"tipo": L["destino_tipo"], "url": L["destino_url"]}, "resumo": resumo(L)}

    @app.get("/api/quiz/{token}")
    def quiz_ver(request: Request, token: str):
        limitar(_ip(request))
        return publico(carregar(token))

    # ---- admin
    @app.get("/admin/funil", response_class=HTMLResponse)
    def admin_funil(request: Request):
        u = me(request, admin=True)
        rows = pg.q("SELECT q.*, (SELECT estado FROM agenda_reservas r WHERE r.token=q.token AND r.estado NOT IN ('cancelada','remarcada') ORDER BY inicio DESC LIMIT 1) AS reserva "
                    "FROM quiz_leads q ORDER BY criado_em DESC LIMIT 200") if pg.ativo() else []
        cont = {r["k"]: r["n"] for r in pg.q("SELECT coalesce(nullif(destino_tipo,''),'em andamento') k, count(*) n FROM quiz_leads GROUP BY 1")} if pg.ativo() else {}
        return page(request, "admin_funil.html", u, rows=rows, cont=cont, rotulos=ROTULOS, banco=pg.ativo())


def _linhas_resumo(L):
    """Resumo do quiz em linhas legiveis (e-mail para o Lucas e descricao da reserva)."""
    out = []
    if L["situacao"]:
        out.append("Situação: " + ROTULOS["situacao"].get(L["situacao"], L["situacao"]))
    if L["objetivo"]:
        out.append("Quer: " + ROTULOS["objetivo"].get(L["objetivo"], L["objetivo"]))
    if L["problema"]:
        out.append("Problema: " + L["problema"])
    if L["tentou"]:
        out.append("Já tentou: " + L["tentou"])
    if L["custo_mes"]:
        out.append("Custo de esperar: " + ROTULOS["custo_mes"].get(L["custo_mes"], L["custo_mes"]))
    if L["prazo"]:
        out.append("Prazo: " + ROTULOS["prazo"].get(L["prazo"], L["prazo"]))
    if L["formato"]:
        out.append("Formato: " + ROTULOS["formato"].get(L["formato"], L["formato"]))
    if L["compromisso"]:
        out.append("Pronto para começar: " + L["compromisso"])
    if L["empresa_porte"]:
        out.append("Empresa: " + ROTULOS["porte"].get(L["empresa_porte"], L["empresa_porte"]) + (" · " + ROTULOS["decisor"].get(L["empresa_decisor"], L["empresa_decisor"]) if L["empresa_decisor"] else ""))
    return out
