#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${AETHERION_ENV_FILE:-$ROOT_DIR/deploy/production.env}"
COMPOSE_FILE="$ROOT_DIR/deploy/compose/docker-compose.prod.yml"

fail() { printf '[FAIL] %s\n' "$1" >&2; exit 1; }

command -v python3 >/dev/null 2>&1 || fail "python3 is required"
command -v docker >/dev/null 2>&1 || fail "Docker is required"
docker compose version >/dev/null 2>&1 || fail "Docker Compose v2 is required"

if [[ ! -f "$ENV_FILE" ]]; then
  cp "$ROOT_DIR/deploy/production.env.example" "$ENV_FILE"
  ENV_FILE="$ENV_FILE" python3 - <<'PY'
import os
import secrets
from pathlib import Path

path = Path(os.environ["ENV_FILE"])
text = path.read_text(encoding="utf-8")
values = {
    "REPLACE_WITH_INDEPENDENT_64_CHAR_HEX_SECRET_KEY": secrets.token_hex(32),
    "REPLACE_WITH_INDEPENDENT_64_CHAR_HEX_JWT_SECRET": secrets.token_hex(32),
    "REPLACE_WITH_64_CHAR_HEX_POSTGRES_PASSWORD": secrets.token_hex(32),
    "REPLACE_WITH_SECURE_POSTGRES_PASSWORD": secrets.token_hex(32),
}
for source, value in values.items():
    text = text.replace(source, value)
path.write_text(text, encoding="utf-8")
PY
  printf '[INFO] Created and initialized %s\n' "$ENV_FILE"
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

[[ "${SECRET_KEY:-}" != REPLACE_* && -n "${SECRET_KEY:-}" ]] || fail "SECRET_KEY is missing or still a placeholder"
[[ "${JWT_SECRET:-}" != REPLACE_* && -n "${JWT_SECRET:-}" ]] || fail "JWT_SECRET is missing or still a placeholder"
[[ "${POSTGRES_PASSWORD:-}" != REPLACE_* && -n "${POSTGRES_PASSWORD:-}" ]] || fail "POSTGRES_PASSWORD is missing or still a placeholder"

python3 "$ROOT_DIR/scripts/production_preflight.py" --env-file "$ENV_FILE" --config
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" config --quiet
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" up -d --build

for attempt in $(seq 1 60); do
  if docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T postgres pg_isready -U "${POSTGRES_USER:-aetherion_user}" -d "${POSTGRES_DB:-aetherion}" >/dev/null 2>&1; then
    printf '[PASS] PostgreSQL ready after %s attempt(s)\n' "$attempt"
    break
  fi
  [[ "$attempt" -lt 60 ]] || fail "PostgreSQL did not become ready"
  sleep 2
done

docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" run --rm db-migrate python /app/scripts/db_migrate.py --up
python3 "$ROOT_DIR/scripts/production_preflight.py" --env-file "$ENV_FILE" --runtime --base-url http://127.0.0.1:5000

printf '\n[SUCCESS] Aetherion AI Platform is 100%% Deployed and Production-Ready!\n'
