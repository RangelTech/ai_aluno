"""Postgres central (banco `aluno`), usado pelos modulos novos: quiz (funil), agenda e prova.
O legado continua em SQLite (db.py). Conexao direta ao Postgres (TLS, `sslmode` vem da URL), com pool pequeno aberto no startup.
Sem DATABASE_URL o modulo fica desligado e as rotas novas respondem 503 `banco_indisponivel`."""
import os
from contextlib import contextmanager

from fastapi import HTTPException

URL = os.environ.get("DATABASE_URL", "")
_POOL = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS quiz_leads (
  token TEXT PRIMARY KEY, criado_em TIMESTAMPTZ NOT NULL DEFAULT now(), atualizado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
  situacao TEXT NOT NULL DEFAULT '', objetivo TEXT NOT NULL DEFAULT '', nome TEXT NOT NULL DEFAULT '', whatsapp TEXT NOT NULL DEFAULT '',
  email TEXT NOT NULL DEFAULT '', utm JSONB NOT NULL DEFAULT '{}'::jsonb, problema TEXT NOT NULL DEFAULT '', tentou TEXT NOT NULL DEFAULT '',
  custo_mes TEXT NOT NULL DEFAULT '', prazo TEXT NOT NULL DEFAULT '', formato TEXT NOT NULL DEFAULT '', compromisso TEXT NOT NULL DEFAULT '',
  empresa_porte TEXT NOT NULL DEFAULT '', empresa_decisor TEXT NOT NULL DEFAULT '', etapa INTEGER NOT NULL DEFAULT 4,
  resultado TEXT NOT NULL DEFAULT '', destino_tipo TEXT NOT NULL DEFAULT '', destino_url TEXT NOT NULL DEFAULT '', quente BOOLEAN NOT NULL DEFAULT false,
  status TEXT NOT NULL DEFAULT 'aberto', lead_id INTEGER, ip TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS ix_quiz_status ON quiz_leads(status, criado_em);
CREATE TABLE IF NOT EXISTS agenda_bloqueios (
  id SERIAL PRIMARY KEY, inicio TIMESTAMPTZ NOT NULL, fim TIMESTAMPTZ NOT NULL, repete_semanal BOOLEAN NOT NULL DEFAULT false,
  motivo TEXT NOT NULL DEFAULT '', criado_em TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS agenda_reservas (
  id SERIAL PRIMARY KEY, codigo TEXT UNIQUE NOT NULL, token TEXT NOT NULL REFERENCES quiz_leads(token), inicio TIMESTAMPTZ NOT NULL, fim TIMESTAMPTZ NOT NULL,
  estado TEXT NOT NULL DEFAULT 'marcada', link_chamada TEXT NOT NULL DEFAULT '', cancel_token TEXT NOT NULL,
  lembrete_24h_em TIMESTAMPTZ, lembrete_1h_em TIMESTAMPTZ, criado_em TIMESTAMPTZ NOT NULL DEFAULT now(), atualizado_em TIMESTAMPTZ NOT NULL DEFAULT now());
DROP INDEX IF EXISTS ux_reserva_inicio;
CREATE UNIQUE INDEX IF NOT EXISTS ux_reserva_inicio_ativa ON agenda_reservas(inicio) WHERE estado IN ('marcada','confirmada');
CREATE INDEX IF NOT EXISTS ix_reserva_token ON agenda_reservas(token);
CREATE TABLE IF NOT EXISTS prova_mapa (
  id SERIAL PRIMARY KEY, uf TEXT NOT NULL, cidade TEXT NOT NULL DEFAULT '', lat DOUBLE PRECISION NOT NULL, lon DOUBLE PRECISION NOT NULL,
  tipo TEXT NOT NULL DEFAULT 'mentorado', origem TEXT NOT NULL DEFAULT 'inicial');
CREATE TABLE IF NOT EXISTS prova_depoimentos (
  id SERIAL PRIMARY KEY, nome TEXT NOT NULL, cargo TEXT NOT NULL DEFAULT '', texto TEXT NOT NULL, foto TEXT NOT NULL DEFAULT '',
  ordem INTEGER NOT NULL DEFAULT 0, origem TEXT NOT NULL DEFAULT 'inicial');
CREATE TABLE IF NOT EXISTS prova_logos (
  id SERIAL PRIMARY KEY, nome TEXT NOT NULL, arquivo TEXT NOT NULL, ordem INTEGER NOT NULL DEFAULT 0, origem TEXT NOT NULL DEFAULT 'inicial');
"""


def ativo():
    return bool(URL)


def exigir():
    if not URL:
        raise HTTPException(503, "banco_indisponivel")


def pool():
    global _POOL
    if _POOL is None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool
        _POOL = ConnectionPool(URL, min_size=1, max_size=5, open=True, kwargs={"row_factory": dict_row, "connect_timeout": 10}, name="aluno")
    return _POOL


def fechar():
    global _POOL
    if _POOL is not None:
        _POOL.close()
        _POOL = None


@contextmanager
def conn():
    with pool().connection() as c:
        yield c  # o pool faz commit ao sair sem excecao e rollback com excecao


def init():
    """Abre o pool e cria as tabelas (idempotente). Falha aqui nao derruba o app: o legado em SQLite segue; as rotas novas dao 503."""
    if not URL:
        return False
    with conn() as c:
        c.execute(SCHEMA)
    return True


def q(sql, args=()):
    with conn() as c:
        return c.execute(sql, args).fetchall()


def one(sql, args=()):
    r = q(sql, args)
    return r[0] if r else None


def x(sql, args=()):
    """Executa; se a frase terminar em RETURNING, devolve o primeiro valor devolvido."""
    with conn() as c:
        cur = c.execute(sql, args)
        if cur.description:
            r = cur.fetchone()
            return list(r.values())[0] if r else None
        return cur.rowcount
