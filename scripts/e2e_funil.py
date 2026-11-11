"""Teste de ponta a ponta do v2 (quiz, agenda, prova, ciclo). Uso: python scripts/e2e_funil.py <URL> <senha-do-master> [token-api]
Receita local (Docker, sem instalar nada no PC):
  docker network create aluno-net; docker run -d --name aluno-pg --network aluno-net -e POSTGRES_USER=aluno -e POSTGRES_PASSWORD=aluno -e POSTGRES_DB=aluno postgres:16-alpine
  docker build -t ai-aluno:dev . ; HASH=$(docker run --rm ai-aluno:dev python -c "import bcrypt;print(bcrypt.hashpw(b'Master#2026',bcrypt.gensalt(4)).decode())")
  MSYS_NO_PATHCONV=1 docker run -d --name aluno-dev --network aluno-net -p 8099:8080 -e MASTER_EMAIL=lucas@rangeltech.net -e MASTER_NAME=Lucas -e "MASTER_PASSWORD_HASH=$HASH" \
    -e PUBLIC_URL=http://localhost:8099 -e SITE_URL=http://localhost:4321 -e CICLO_TESTE=1 -e PRODUCTS_API_TOKEN=tok-teste -e DATABASE_URL=postgresql://aluno:aluno@aluno-pg:5432/aluno ai-aluno:dev
  python scripts/e2e_funil.py http://localhost:8099 'Master#2026' tok-teste
  docker rm -f aluno-dev aluno-pg; docker network rm aluno-net"""
import http.cookiejar, json, re, sys, threading, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone

URL = sys.argv[1].rstrip("/")
SENHA = sys.argv[2]
TOK = sys.argv[3] if len(sys.argv) > 3 else "tok-teste"
falhas = []
def ok(c, msg):
    print(("ok   " if c else "FALHA") + " " + msg)
    if not c: falhas.append(msg)

jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k): return None
opener_nr = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), NoRedirect)

def req(method, path, data=None, headers=None, form=None, raw=None, nr=False):
    h = {"User-Agent": "e2e"}
    if headers: h.update(headers)
    body = None
    if data is not None: body = json.dumps(data).encode(); h["Content-Type"] = "application/json"
    if form is not None: body = urllib.parse.urlencode(form).encode(); h["Content-Type"] = "application/x-www-form-urlencoded"
    if raw is not None: body = raw
    r = urllib.request.Request(URL + path, data=body, headers=h, method=method)
    try:
        resp = (opener_nr if nr else opener).open(r, timeout=30)
        return resp.status, resp.read().decode("utf-8", "replace"), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), dict(e.headers)

def j(method, path, data=None, headers=None):
    s, b, h = req(method, path, data, headers)
    try: return s, json.loads(b), h
    except ValueError: return s, b, h

AGORA = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)  # 09:00 em Brasilia
iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")

def quiz(situacao, objetivo, extra, nome="Ana Teste"):
    s, r, _ = j("POST", "/api/quiz", {"situacao": situacao, "objetivo": objetivo, "nome": nome, "whatsapp": "62985613482", "email": f"{nome.split()[0].lower()}@example.com", "origem": {"utm_source": "instagram"}, "site": ""})
    ok(s == 201 and r.get("token"), f"quiz criado ({situacao}/{objetivo})")
    t = r["token"]
    s, r, _ = j("PATCH", f"/api/quiz/{t}", extra)
    ok(s == 200, "respostas gravadas")
    s, r, _ = j("POST", f"/api/quiz/{t}/concluir")
    ok(s == 200 and r.get("destino"), "quiz concluido")
    return t, r

# ---- quiz: 4 destinos
t_ag, r = quiz("empregado", "saas", {"problema": "Quero um SaaS que responda clientes no WhatsApp", "custo_mes": "2000a10000", "prazo": "30dias", "formato": "cara_a_cara", "compromisso": "sim"})
ok(r["resultado"] == "cara_a_cara" and r["destino"]["tipo"] == "agenda" and f"/agendar?t={t_ag}" in r["destino"]["url"] and r["quente"], "cara a cara + 30 dias -> agenda (quente)")
t_ck, r = quiz("autonomo", "renda", {"problema": "Trocar hora por dinheiro cansa", "custo_mes": "500a2000", "prazo": "3meses", "formato": "whatsapp", "compromisso": "nao"}, "Bruno Teste")
ok(r["resultado"] == "whatsapp" and r["destino"]["tipo"] == "checkout" and "cakto" in r["destino"]["url"], "whatsapp -> checkout")
t_em, r = quiz("empresa", "atendimento", {"problema": "Atendimento nao da conta", "empresa": {"porte": "11a50", "decisor": "sim"}, "custo_mes": "mais10000", "prazo": "semana"}, "Carla Teste")
ok(r["resultado"] == "empresa" and r["destino"]["tipo"] == "whatsapp" and "wa.me" in r["destino"]["url"] and "11 a 50" in urllib.parse.unquote(r["destino"]["url"]), "empresa -> whatsapp com resumo")
t_fr, r = quiz("desempregado", "aprender", {"problema": "Quero aprender", "custo_mes": "naosei", "prazo": "pesquisando", "formato": "whatsapp"}, "Dani Teste")
ok(r["resultado"] == "frio" and r["destino"]["tipo"] == "obrigado", "pesquisando -> obrigado")
s, r, _ = j("GET", f"/api/quiz/{t_ag}")
ok(s == 200 and r.get("problema") and "ip" not in r and "lead_id" not in r, "GET quiz devolve respostas sem ip/lead_id")
s, r, _ = j("POST", "/api/quiz", {"situacao": "empregado", "nome": "Robo", "whatsapp": "62985613482", "email": "r@example.com", "site": "http://spam"})
ok(s == 400, "campo armadilha bloqueia robo")
s, r, _ = j("POST", "/api/quiz", {"situacao": "empregado", "nome": "X", "whatsapp": "123", "email": "nao-e-email"})
ok(s == 400, "validacao de contato")
s, b, h = req("OPTIONS", "/api/quiz", headers={"Origin": "http://localhost:4321", "Access-Control-Request-Method": "POST"})
ok(h.get("access-control-allow-origin") == "http://localhost:4321", "CORS para a origem do site")
s, b, h = req("OPTIONS", "/api/quiz", headers={"Origin": "https://mal.example", "Access-Control-Request-Method": "POST"})
ok(not h.get("access-control-allow-origin"), "CORS nega outra origem")

# ---- ciclo: lead quiz aberto para o frio/checkout; agenda fecha
s, r, _ = j("GET", "/api/ciclo/status", headers={"Authorization": "Bearer " + TOK})
ok(s == 200 and r["leads"].get("quiz:aberto", 0) >= 1, f"lead quiz no motor de ciclo: {r.get('leads')}")
s, r, _ = j("POST", f"/api/ciclo/executar?dry=1&agora={iso(AGORA + timedelta(hours=2))}", headers={"Authorization": "Bearer " + TOK})
itens = [i for i in r.get("itens", []) if i.get("tipo") == "quiz"]
ok(s == 200 and any(i.get("acao") == "enviaria" and i.get("passo") == "retomar" for i in itens), f"ciclo (dry) enviaria 'retomar' ao lead do quiz: {itens[:2]}")

# ---- agenda: horarios
s, r, _ = j("GET", f"/api/agenda/horarios?t={t_ag}&agora={iso(AGORA)}")
d0 = r["dias"][0]
ok(s == 200 and len(r["dias"]) == 7 and d0["horarios"][0] == "11:00" and d0["horarios"][-1] == "21:30", f"horarios: 7 dias, hoje de {d0['horarios'][:1]} a {d0['horarios'][-1:]} (antecedencia 2 h, janela 7h30-22h)")
ok(r["dias"][1]["horarios"][0] == "07:30", "amanha comeca 07:30")
s, r, _ = j("GET", f"/api/agenda/horarios?t={t_ck}")
ok(s == 403, "token sem resultado cara a cara nao ve a agenda")

# ---- admin: login, bloqueio, prova
s, b, h = req("POST", "/login", form={"email": "lucas@rangeltech.net", "password": SENHA}, nr=True)
ok(s == 303, "login do master")
s, b, _ = req("GET", "/admin/agenda")
csrf = re.search(r'name="csrf" value="([^"]+)"', b).group(1)
dia = d0["data"]
s, b, _ = req("POST", "/admin/agenda/bloqueio", form={"csrf": csrf, "inicio": f"{dia}T14:00", "fim": f"{dia}T15:00", "motivo": "teste"}, nr=True)
ok(s == 303, "bloqueio criado")
s, r, _ = j("GET", f"/api/agenda/horarios?t={t_ag}&agora={iso(AGORA)}")
hs = r["dias"][0]["horarios"]
ok("14:00" not in hs and "14:30" not in hs and "13:30" in hs and "15:00" in hs, f"bloqueio 14-15h some dos horarios ({[x for x in hs if x.startswith('1')][:8]})")

# ---- reserva concorrente: dois leads cara a cara no mesmo horario
t_ag2, _ = quiz("empregado", "tempo", {"problema": "Perco horas em planilha", "custo_mes": "ate500", "prazo": "semana", "formato": "cara_a_cara", "compromisso": "sim"}, "Edu Teste")
res = {}
def tenta(k, t):
    res[k] = j("POST", f"/api/agenda/reservar?agora={iso(AGORA)}", {"t": t, "inicio": f"{dia}T16:00"})
th = [threading.Thread(target=tenta, args=("a", t_ag)), threading.Thread(target=tenta, args=("b", t_ag2))]
[x.start() for x in th]; [x.join() for x in th]
codes = sorted([res["a"][0], res["b"][0]])
ok(codes == [201, 409], f"reserva concorrente: uma 201 e uma 409 ({codes})")
ganhou = res["a"] if res["a"][0] == 201 else res["b"]
t_win = t_ag if res["a"][0] == 201 else t_ag2
codigo, url_cancelar = ganhou[1]["codigo"], ganhou[1]["url_cancelar"]
s, r, _ = j("GET", f"/api/agenda/horarios?t={t_ag2}&agora={iso(AGORA)}")
hs = r["dias"][0]["horarios"]
ok("16:00" not in hs and "15:30" not in hs and "16:30" not in hs and "15:00" in hs and "17:00" in hs, "folga de 15 min em volta da reserva")
s, r, _ = j("POST", f"/api/agenda/reservar?agora={iso(AGORA)}", {"t": t_ag2, "inicio": f"{dia}T09:30"})
ok(s == 409, "antecedencia minima de 2 h respeitada")
s, b, _ = req("GET", f"/agendar?t={t_win}")
ok(s == 200 and "marcado" in b.lower(), "pagina /agendar mostra a reserva existente")
# cancelar pelo link
k = urllib.parse.parse_qs(urllib.parse.urlparse(url_cancelar).query)["k"][0]
s, b, _ = req("POST", f"/agenda/{codigo}/cancelar", form={"k": k}, nr=True)
ok(s == 303, "cancelamento pelo link do e-mail")
s, r, _ = j("GET", f"/api/agenda/horarios?t={t_ag2}&agora={iso(AGORA)}")
ok("16:00" in r["dias"][0]["horarios"], "horario volta a ficar livre")
s, b, _ = req("GET", f"/agenda/{codigo}?k=errado")
ok(s == 404, "codigo com chave errada: 404")

# ---- falta automatica: nova reserva, viaja no tempo
s, r, _ = j("POST", f"/api/agenda/reservar?agora={iso(AGORA)}", {"t": t_ag2, "inicio": f"{dia}T17:00"})
ok(s == 201, "nova reserva 17:00")
cod2 = r["codigo"]
s, r, _ = j("POST", f"/api/agenda/rodar?agora={iso(AGORA + timedelta(hours=7, minutes=30))}", headers={"Authorization": "Bearer " + TOK})
ok(s == 200 and r.get("lembrete_1h") == 1, f"lembrete de 1 h contado ({r})")
s, r, _ = j("POST", f"/api/agenda/rodar?agora={iso(AGORA + timedelta(hours=11))}", headers={"Authorization": "Bearer " + TOK})
ok(s == 200 and r.get("faltou") == 1, f"falta automatica 2 h depois do fim ({r})")
s, r, _ = j("GET", "/api/ciclo/status", headers={"Authorization": "Bearer " + TOK})
ok(r["leads"].get("faltou:aberto", 0) >= 1, f"lead 'faltou' aberto na recuperacao: {r['leads']}")
s, r, _ = j("GET", f"/api/quiz/{t_ag2}")
ok(r.get("status") == "faltou", "quiz marcado como faltou")
s, b, _ = req("GET", "/admin/agenda")
ok(s == 200 and "faltou" in b and "Edu" in b, "painel da agenda lista a reserva")
s, b, _ = req("GET", "/admin/funil")
ok(s == 200 and "Ana" in b and "Carla" in b, "painel do funil lista os leads")

# ---- prova: importar, ler, desligar
dados = {"mapa": [{"uf": "GO", "cidade": "Goiânia", "lat": -16.68, "lon": -49.25, "tipo": "mentorado"}, {"uf": "SP", "cidade": "São Paulo", "lat": -23.55, "lon": -46.63, "tipo": "mentorado"}, {"uf": "SP", "cidade": "Osasco", "lat": -23.53, "lon": -46.79, "tipo": "empresa"}],
         "depoimentos": [{"nome": "Teste T.", "cargo": "Mentoria · Goiânia", "texto": "Resolveu.", "foto": ""}], "logos": [{"nome": "Empresa", "arquivo": "x.svg"}]}
bnd = "----e2e"
corpo = (f"--{bnd}\r\nContent-Disposition: form-data; name=\"csrf\"\r\n\r\n{csrf}\r\n--{bnd}\r\nContent-Disposition: form-data; name=\"arquivo\"; filename=\"prova.json\"\r\nContent-Type: application/json\r\n\r\n{json.dumps(dados)}\r\n--{bnd}--\r\n").encode()
s, b, _ = req("POST", "/admin/prova/importar", raw=corpo, headers={"Content-Type": f"multipart/form-data; boundary={bnd}"}, nr=True)
ok(s == 303, "importacao da prova")
s, r, h = j("GET", "/api/prova")
ok(s == 200 and r["numeros"] == {"mentorados": 2, "estados": 2, "empresas": 1} and len(r["depoimentos"]) == 1 and "origem" not in json.dumps(r), f"GET /api/prova: {r.get('numeros')} sem 'origem'")
s, b, _ = req("POST", "/admin/prova/publica", form={"csrf": csrf}, nr=True)
s, r, _ = j("GET", "/api/prova")
ok(r["numeros"]["mentorados"] == 0, "exposicao desligada devolve vazio")
s, b, _ = req("POST", "/admin/prova/publica", form={"csrf": csrf, "ligar": "1"}, nr=True)
s, r, _ = j("GET", "/api/prova")
ok(r["numeros"]["mentorados"] == 2, "exposicao religada")
s, b, _ = req("GET", "/admin/prova")
ok(s == 200 and "inicial" in b, "painel da prova")

print(f"\n{len(falhas)} falha(s).")
sys.exit(1 if falhas else 0)
