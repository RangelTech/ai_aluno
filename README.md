# ai_aluno: área do aluno (user.rangeltech.net)

Plataforma própria de Lucas Rangel (Rangel Tech): cursos em vídeo (HLS), downloads protegidos, mentorias ao vivo e gravações privadas.
Implementa o núcleo do MASTER_PLAN V3 (personal-skills/docs). Sem Cloudflare por enquanto: arquivos e vídeos ficam no volume `/data` do servidor.

- **Stack**: FastAPI + Jinja + SQLite (WAL) + ffmpeg. Sem CDN, sem script de terceiros. hls.js vendorizado.
- **Acesso**: sessão por cookie HttpOnly, bcrypt, CSRF, limite de tentativas; papéis admin/mentor/aluno; entitlements com origem, validade e auditoria.
- **Vídeo**: upload → ffmpeg → HLS 720p; a playlist e cada segmento revalidam sessão + acesso (403 sem direito, 401 sem login).
- **Mentorias**: agenda do admin, janela de entrada (10 min antes / 30 min depois, ajustável), sala WebRTC em malha até 5 pessoas, sinalização por WebSocket próprio, chat, compartilhamento de tela, consentimento de gravação registrado e gravações com grant por aluno.
- **Kiwify**: `POST /webhooks/kiwify?signature=` (HMAC-SHA1, idempotente). Ativa quando `KIWIFY_TOKEN` existir no ambiente.

## Ambiente
`MASTER_EMAIL`, `MASTER_PASSWORD_HASH` (bcrypt), `MASTER_NAME`, `DATA_DIR`, `ICE_SERVERS_JSON` (STUN/TURN; padrão STUN público), `KIWIFY_TOKEN`, `APP_TZ`.

## Pendências assumidas (ver docs do plano)
Gravação server-side (Jibri), R2 privado, TURN próprio, e-mails de lembrete, pagamento real. Hoje a gravação é enviada pelo admin depois da sessão.

## Release
Workflow `Release` (dispatch com versão) gera `image.tar.gz` + `release-manifest.json`; o deploy é só pelo `vps_rt_infra` (`service=ai-aluno`).
