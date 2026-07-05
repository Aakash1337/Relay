#!/usr/bin/env bash
# RELAY VM startup. Runs on EVERY boot (GCE metadata startup-script):
#   1. install Docker if missing
#   2. fetch secrets from Secret Manager into a tmpfs env file (mode 600)
#   3. log in to Artifact Registry with the VM's own identity token
#   4. write the compose file and bring the stack up
# Idempotent by construction; the migrate service is idempotent too.
set -euo pipefail
exec > /var/log/relay-startup.log 2>&1
echo "=== relay startup $(date -u +%FT%TZ) ==="

# --- 1. Docker -------------------------------------------------------------
if ! command -v docker > /dev/null; then
  apt-get update -q
  DEBIAN_FRONTEND=noninteractive apt-get install -yq docker.io docker-compose-v2
  systemctl enable --now docker
fi

# --- 2. Secrets → tmpfs env file --------------------------------------------
# /run is tmpfs on Ubuntu: the assembled env file never touches disk.
install -d -m 700 /run/relay
ENV_FILE=/run/relay/relay.env
: > "$ENV_FILE"
chmod 600 "$ENV_FILE"

MD="http://metadata.google.internal/computeMetadata/v1"
token() {
  curl -sf -H "Metadata-Flavor: Google" \
    "$MD/instance/service-accounts/default/token" \
    | python3 -c 'import sys, json; print(json.load(sys.stdin)["access_token"])'
}

# fetch_secret NAME → prints the latest version, or fails (rc 1) if absent.
fetch_secret() {
  curl -sf -H "Authorization: Bearer $(token)" \
    "https://secretmanager.googleapis.com/v1/projects/${project_id}/secrets/$1/versions/latest:access" \
    | python3 -c 'import sys, json, base64; print(base64.b64decode(json.load(sys.stdin)["payload"]["data"]).decode(), end="")'
}

put() { printf '%s=%s\n' "$1" "$2" >> "$ENV_FILE"; }

require_secret() {
  local var="$1" name="$2" val
  if ! val="$(fetch_secret "$name")"; then
    echo "FATAL: required secret '$name' has no version — add one with:" >&2
    echo "  printf '%s' 'VALUE' | gcloud secrets versions add $name --data-file=-" >&2
    exit 1
  fi
  put "$var" "$val"
}

optional_secret() {
  local var="$1" name="$2" val
  if val="$(fetch_secret "$name")"; then
    put "$var" "$val"
  else
    echo "note: optional secret '$name' not set; skipping"
  fi
}

require_secret RELAY_ADMIN_TOKEN       relay-admin-token
require_secret RELAY_MASTER_KEY        relay-master-key
require_secret RELAY_EMAIL_HASH_PEPPER relay-email-hash-pepper
optional_secret AWS_ACCESS_KEY_ID      relay-aws-access-key-id
optional_secret AWS_SECRET_ACCESS_KEY  relay-aws-secret-access-key
optional_secret RELAY_GOOGLE_API_KEY   relay-google-api-key
optional_secret N8N_ENCRYPTION_KEY     relay-n8n-encryption-key

DB_PASS="$(fetch_secret relay-db-password)"
APP_DB_PASS="$(fetch_secret relay-app-db-password)"
put RELAY_DATABASE_URL     "postgresql+psycopg://relay:$${DB_PASS}@${db_host}:5432/relay?sslmode=require"
put RELAY_APP_DATABASE_URL "postgresql+psycopg://relay_app:$${APP_DB_PASS}@${db_host}:5432/relay?sslmode=require"
put RELAY_APP_DB_PASSWORD  "$APP_DB_PASS"

put RELAY_IMAGE "${image}"
%{ if enable_tunnel ~}
require_secret TUNNEL_TOKEN relay-tunnel-token
PROFILES="--profile tunnel"
%{ else ~}
PROFILES=""
%{ endif ~}

# Non-secret app config from Terraform's app_env map:
%{ for k, v in app_env ~}
put ${k} '${v}'
%{ endfor ~}

# --- 3. Registry login -------------------------------------------------------
token | docker login -u oauth2accesstoken --password-stdin "https://${registry_host}"

# --- 4. Compose up -----------------------------------------------------------
install -d /opt/relay
cat > /opt/relay/docker-compose.yml <<'RELAY_COMPOSE_EOF'
${compose_yaml}
RELAY_COMPOSE_EOF

cd /opt/relay
# $PROFILES is intentionally unquoted (empty or "--profile tunnel").
docker compose $PROFILES --env-file "$ENV_FILE" pull
docker compose $PROFILES --env-file "$ENV_FILE" up -d --remove-orphans
echo "=== relay startup complete ==="
