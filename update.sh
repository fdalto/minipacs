#!/usr/bin/env bash
set -Eeuo pipefail
ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"
[[ -f .env ]] || { echo "ERRO: .env não existe. Execute bash install.sh primeiro." >&2; exit 1; }
docker compose build
docker compose up -d
for _ in {1..20}; do curl -fsS http://127.0.0.1:8787/health >/dev/null && { echo "Atualização concluída; healthcheck OK."; exit 0; }; sleep 2; done
echo "ERRO: healthcheck falhou. Execute ./manage.sh logs web" >&2; exit 1
