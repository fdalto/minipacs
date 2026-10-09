#!/usr/bin/env bash
set -Eeuo pipefail
ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"; cd "$ROOT_DIR"
case "${1:-}" in
  status) docker compose ps ;;
  logs) if [[ $# -gt 1 ]]; then docker compose logs --tail=200 -f "$2"; else docker compose logs --tail=200 -f; fi ;;
  restart) docker compose restart ;;
  stop) docker compose stop ;;
  start) docker compose up -d ;;
  health) curl -fsS http://127.0.0.1:8787/health; echo ;;
  usage) docker compose exec -T web python -c "from app.db import summary; from app.config import load_settings; import json; print(json.dumps(summary(load_settings().db_path), indent=2))"; du -sh data db tmp ;;
  *) echo "Uso: $0 {status|logs [web|dicom-receiver|cleanup]|restart|stop|start|health|usage}" >&2; exit 2 ;;
esac
