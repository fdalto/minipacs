#!/usr/bin/env bash
# MiniPACS installer for Ubuntu. Run as: bash install.sh
set -Eeuo pipefail
ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
APP_UID=10001
APP_GID=10001
ENV_FILE="$ROOT_DIR/.env"
NGINX_AVAILABLE=/etc/nginx/sites-available/minipacs
NGINX_ENABLED=/etc/nginx/sites-enabled/minipacs

die() { echo "ERRO: $*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "Comando ausente: $1"; }
[[ $EUID -eq 0 ]] || die "Execute como root: sudo bash install.sh"
[[ "$(uname -s)" == "Linux" ]] || die "Este instalador é destinado a Ubuntu/Linux."
[[ -f /etc/os-release ]] && . /etc/os-release
[[ "${ID:-}" == "ubuntu" || "${ID_LIKE:-}" == *debian* ]] || echo "Aviso: distribuição não identificada como Ubuntu; continuando com cuidado."
need docker
docker compose version >/dev/null 2>&1 || die "Docker Compose v2 não encontrado (docker compose)."
need nginx
need curl
need openssl

for dir in data db tmp logs; do install -d -m 0750 -o "$APP_UID" -g "$APP_GID" "$ROOT_DIR/$dir"; done
install -d -m 0755 -o root -g root "$ROOT_DIR/config"

if [[ ! -f "$ENV_FILE" ]]; then
  read -r -p "Domínio público do MiniPACS (ex.: minipacs.exemplo.com): " DOMAIN
  [[ "$DOMAIN" =~ ^[A-Za-z0-9.-]+$ ]] && [[ -n "$DOMAIN" ]] || die "Domínio inválido."
  read -r -p "AE Title local [MINIPACS]: " DICOM_AE_TITLE; DICOM_AE_TITLE="${DICOM_AE_TITLE:-MINIPACS}"
  read -r -p "Calling AE Title(s) permitido(s), separados por vírgula: " ALLOWED_CALLING_AE
  [[ -n "$ALLOWED_CALLING_AE" ]] || die "ALLOWED_CALLING_AE não pode ficar vazio."
  read -r -p "Porta DICOM externa [11112]: " DICOM_PORT; DICOM_PORT="${DICOM_PORT:-11112}"
  [[ "$DICOM_PORT" =~ ^[0-9]{1,5}$ ]] && (( DICOM_PORT > 0 && DICOM_PORT < 65536 )) || die "Porta DICOM inválida."
  read -r -p "Usuário administrador [admin]: " ADMIN_USERNAME; ADMIN_USERNAME="${ADMIN_USERNAME:-admin}"
  read -r -s -p "Senha do administrador: " ADMIN_PASSWORD; echo
  [[ ${#ADMIN_PASSWORD} -ge 12 ]] || die "Use uma senha com pelo menos 12 caracteres."
  echo "Construindo imagem para gerar o hash Argon2..."
  docker build -t minipacs:local "$ROOT_DIR"
  ADMIN_PASSWORD_HASH="$(printf '%s\n' "$ADMIN_PASSWORD" | docker run --rm -i minipacs:local python -m app.password_hash)"
  unset ADMIN_PASSWORD
  SESSION_SECRET="$(openssl rand -hex 32)"
  umask 077
  # Single quotes prevent Docker Compose from interpolating the '$' in an Argon2 hash.
  printf "DOMAIN=%s\nDICOM_AE_TITLE=%s\nALLOWED_CALLING_AE=%s\nDICOM_PORT=%s\nDICOM_BIND_ADDRESS=0.0.0.0\nADMIN_USERNAME=%s\nADMIN_PASSWORD_HASH='%s'\nSESSION_SECRET=%s\nRETENTION_DAYS=15\nCOOKIE_SECURE=true\nMAX_BULK_STUDIES=20\nMAX_BULK_BYTES=21474836480\nMAX_DICOM_FILE_BYTES=536870912\nEXTERNAL_API_TOKEN_HASH=\nEXTERNAL_API_TOKEN_NAME=lovable-backend\nEXTERNAL_API_ALLOWED_ORIGINS=\nEXTERNAL_API_RATE_LIMIT_PER_MINUTE=60\n" "$DOMAIN" "$DICOM_AE_TITLE" "$ALLOWED_CALLING_AE" "$DICOM_PORT" "$ADMIN_USERNAME" "$ADMIN_PASSWORD_HASH" "$SESSION_SECRET" > "$ENV_FILE"
  chown root:root "$ENV_FILE"; chmod 600 "$ENV_FILE"
else
  echo "Usando .env existente; dados e credenciais serão preservados."
fi

get_env() { sed -n "s/^$1=//p" "$ENV_FILE" | head -n 1; }
DOMAIN="$(get_env DOMAIN)"
DICOM_PORT="$(get_env DICOM_PORT)"
DICOM_AE_TITLE="$(get_env DICOM_AE_TITLE)"
[[ -n "$DOMAIN" ]] || die "DOMAIN não encontrado em .env"
if [[ -f "$NGINX_AVAILABLE" ]]; then cp -a "$NGINX_AVAILABLE" "$NGINX_AVAILABLE.bak.$(date +%Y%m%d%H%M%S)"; fi

render_nginx() { sed "s/__DOMAIN__/$DOMAIN/g" "$1" > "$NGINX_AVAILABLE"; ln -sfn "$NGINX_AVAILABLE" "$NGINX_ENABLED"; nginx -t; systemctl reload nginx; }
CERT_DIR="/etc/letsencrypt/live/$DOMAIN"
if [[ -f "$CERT_DIR/fullchain.pem" && -f "$CERT_DIR/privkey.pem" ]]; then
  render_nginx "$ROOT_DIR/config/nginx-https.conf.template"
  HTTPS_READY=yes
else
  echo "Certificado ainda não encontrado; instalando configuração HTTP temporária, válida para ACME."
  render_nginx "$ROOT_DIR/config/nginx-http.conf.template"
  HTTPS_READY=no
  if command -v certbot >/dev/null 2>&1; then
    echo "Tentando emitir certificado Let’s Encrypt (o DNS precisa apontar para esta VPS)."
    if certbot certonly --webroot -w /var/www/html -d "$DOMAIN" --non-interactive --agree-tos --register-unsafely-without-email; then
      render_nginx "$ROOT_DIR/config/nginx-https.conf.template"; HTTPS_READY=yes
    else
      echo "AVISO: não foi possível emitir o certificado agora. O MiniPACS continua instalado em HTTP temporário."
      echo "Após configurar o DNS, execute: certbot certonly --webroot -w /var/www/html -d $DOMAIN"
      echo "Depois execute novamente: bash install.sh"
    fi
  else
    echo "AVISO: certbot não está instalado. Depois do DNS: instale certbot, emita o certificado e rode bash install.sh novamente."
  fi
fi

echo "Construindo e iniciando serviços..."
cd "$ROOT_DIR"
docker compose build
docker compose up -d
for _ in {1..20}; do
  if curl --fail --silent http://127.0.0.1:8787/health >/dev/null; then break; fi
  sleep 2
done
curl --fail --silent http://127.0.0.1:8787/health >/dev/null || die "Healthcheck local falhou; veja: docker compose logs"
if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  echo "UFW está ativo. Libere manualmente a porta DICOM se necessário: ufw allow ${DICOM_PORT:-11112}/tcp"
fi
echo
echo "MiniPACS instalado. Web interno: http://127.0.0.1:8787/"
echo "DICOM: 0.0.0.0:${DICOM_PORT:-11112} | Called AE: ${DICOM_AE_TITLE:-MINIPACS}"
[[ "$HTTPS_READY" == yes ]] && echo "Site: https://$DOMAIN" || echo "HTTPS pendente; siga a instrução acima antes de uso externo."
