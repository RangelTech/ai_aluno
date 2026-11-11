"""Prova social do site (mapa, numeros, depoimentos, logos), servida pelo banco. Spec SITE_V2, secoes 15.1 e 17.3.
A coluna `origem` (inicial | real) nunca sai pela API. Nenhum dado fica no repositorio: entra por importacao no painel."""
import json
import time

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from . import core, pg

_cache = {"em": 0, "dados": None}
CACHE_S = 300
VAZIO = {"mapa": [], "numeros": {"mentorados": 0, "estados": 0, "empresas": 0}, "depoimentos": [], "logos": []}


def montar():
    mapa = pg.q("SELECT uf, cidade, lat, lon, tipo FROM prova_mapa ORDER BY id")
    deps = pg.q("SELECT nome, cargo, texto, foto FROM prova_depoimentos ORDER BY ordem, id")
    logos = pg.q("SELECT nome, arquivo FROM prova_logos ORDER BY ordem, id")
    return {"mapa": mapa, "numeros": {"mentorados": sum(1 for p in mapa if p["tipo"] == "mentorado"), "estados": len({p["uf"] for p in mapa}),
                                      "empresas": sum(1 for p in mapa if p["tipo"] == "empresa")}, "depoimentos": deps, "logos": logos}


def dados(extras):
    if extras.setting("prova_publica", "1") != "1":
        return VAZIO
    if _cache["dados"] is None or time.time() - _cache["em"] > CACHE_S:
        _cache.update(em=time.time(), dados=montar())
    return _cache["dados"]


def limpar_cache():
    _cache.update(em=0, dados=None)


def importar(d):
    """Substitui tudo por `origem='inicial'`. Formato: {mapa:[{uf,cidade,lat,lon,tipo}], depoimentos:[{nome,cargo,texto,foto}], logos:[{nome,arquivo}]}."""
    mapa, deps, logos = d.get("mapa") or [], d.get("depoimentos") or [], d.get("logos") or []
    for p in mapa:
        if not (isinstance(p, dict) and len(str(p.get("uf", ""))) == 2):
            raise ValueError("mapa: cada ponto precisa de uf (2 letras), lat e lon")
        float(p["lat"]), float(p["lon"])
    with pg.conn() as c:
        c.execute("DELETE FROM prova_mapa; DELETE FROM prova_depoimentos; DELETE FROM prova_logos")
        for p in mapa:
            c.execute("INSERT INTO prova_mapa(uf,cidade,lat,lon,tipo,origem) VALUES (%s,%s,%s,%s,%s,'inicial')",
                      (str(p["uf"]).upper(), str(p.get("cidade") or "")[:80], float(p["lat"]), float(p["lon"]), "empresa" if p.get("tipo") == "empresa" else "mentorado"))
        for i, x in enumerate(deps):
            c.execute("INSERT INTO prova_depoimentos(nome,cargo,texto,foto,ordem,origem) VALUES (%s,%s,%s,%s,%s,'inicial')",
                      (str(x.get("nome") or "")[:80], str(x.get("cargo") or "")[:120], str(x.get("texto") or "")[:1200], str(x.get("foto") or "")[:300], i))
        for i, x in enumerate(logos):
            c.execute("INSERT INTO prova_logos(nome,arquivo,ordem,origem) VALUES (%s,%s,%s,'inicial')", (str(x.get("nome") or "")[:80], str(x.get("arquivo") or "")[:200], i))
    limpar_cache()
    return len(mapa), len(deps), len(logos)


def setup(app, me, page, form, back, T, extras):
    @app.get("/api/prova")
    def api_prova(request: Request):
        pg.exigir()
        return JSONResponse(dados(extras), headers={"Cache-Control": "public, max-age=300"})

    @app.post("/api/prova/importar")
    async def api_prova_importar(request: Request):
        """Importacao por automacao (Bearer PRODUCTS_API_TOKEN), mesmo formato do painel."""
        import hmac
        import os
        pg.exigir()
        tok = os.environ.get("PRODUCTS_API_TOKEN", "")
        got = request.headers.get("authorization", "")
        if not tok or not got.lower().startswith("bearer ") or not hmac.compare_digest(got[7:].strip(), tok):
            raise HTTPException(401, "token_invalido")
        try:
            n = importar(await request.json())
        except (ValueError, AttributeError, KeyError, TypeError) as e:
            raise HTTPException(400, f"invalido: {str(e)[:120]}")
        core.audit(None, "prova_importada_api", f"mapa={n[0]} depoimentos={n[1]} logos={n[2]}")
        return {"ok": True, "mapa": n[0], "depoimentos": n[1], "logos": n[2]}

    @app.get("/admin/prova", response_class=HTMLResponse)
    def admin_prova(request: Request):
        u = me(request, admin=True)
        cont = {}
        if pg.ativo():
            for t in ("prova_mapa", "prova_depoimentos", "prova_logos"):
                cont[t] = {r["origem"]: r["n"] for r in pg.q(f"SELECT origem, count(*) n FROM {t} GROUP BY origem")}
            cont["numeros"] = montar()["numeros"]
        return page(request, "admin_prova.html", u, banco=pg.ativo(), cont=cont, publica=extras.setting("prova_publica", "1") == "1")

    @app.post("/admin/prova/importar")
    async def admin_prova_importar(request: Request):
        u = me(request, admin=True)
        f = await form(request, u)
        pg.exigir()
        up = f.get("arquivo")
        try:
            raw = await up.read() if hasattr(up, "read") else b""
            n = importar(json.loads(raw.decode("utf-8")))
        except (ValueError, AttributeError, KeyError, TypeError) as e:
            return back("/admin/prova", erro=f"Arquivo inválido: {str(e)[:120]}")
        core.audit(u["id"], "prova_importada", f"mapa={n[0]} depoimentos={n[1]} logos={n[2]}")
        return back("/admin/prova", ok=f"Importado: {n[0]} pontos, {n[1]} depoimentos, {n[2]} logos.")

    @app.post("/admin/prova/publica")
    async def admin_prova_publica(request: Request):
        u = me(request, admin=True)
        f = await form(request, u)
        extras.set_setting("prova_publica", "1" if f.get("ligar") else "0")
        limpar_cache()
        core.audit(u["id"], "prova_publica", "1" if f.get("ligar") else "0")
        return back("/admin/prova", ok="Exposição atualizada.")
