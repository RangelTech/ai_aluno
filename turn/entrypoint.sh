#!/bin/sh
# coturn com autenticacao temporaria (REST API: usuario = validade:id, senha = HMAC-SHA1 do segredo).
set -eu
: "${TURN_SECRET:?TURN_SECRET ausente}"
: "${TURN_EXTERNAL_IP:?TURN_EXTERNAL_IP ausente}"
MINP="${TURN_MIN_PORT:-49160}"
MAXP="${TURN_MAX_PORT:-49200}"
# Nunca repassa para redes privadas/loopback: evita usar o relay para alcancar a rede interna da VPS.
exec turnserver -n \
  --log-file=stdout --simple-log \
  --realm="${TURN_REALM:-rangeltech.net}" \
  --use-auth-secret --static-auth-secret="$TURN_SECRET" \
  --external-ip="$TURN_EXTERNAL_IP" \
  --listening-port=3478 --min-port="$MINP" --max-port="$MAXP" \
  --fingerprint --no-cli --no-tls --no-dtls --no-multicast-peers \
  --denied-peer-ip=0.0.0.0-0.255.255.255 \
  --denied-peer-ip=10.0.0.0-10.255.255.255 \
  --denied-peer-ip=100.64.0.0-100.127.255.255 \
  --denied-peer-ip=127.0.0.0-127.255.255.255 \
  --denied-peer-ip=169.254.0.0-169.254.255.255 \
  --denied-peer-ip=172.16.0.0-172.31.255.255 \
  --denied-peer-ip=192.168.0.0-192.168.255.255 \
  --total-quota=200 --user-quota=24 --stale-nonce=600
