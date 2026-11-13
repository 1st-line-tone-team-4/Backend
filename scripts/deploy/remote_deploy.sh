#!/usr/bin/env bash
# Runs on the deploy server. Usage: remote_deploy.sh <bundle-path> <commit-sha>
#
# The server checkout is updated from a git bundle sent by the workflow, so the
# server needs no GitHub credentials. On a failed health check the previous
# commit is restored and the services restarted.
set -euo pipefail

BUNDLE="$1"
SHA="$2"
APP_DIR="${APP_DIR:-/home/ubuntu/Backend}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8000/health}"
SERVICES=(team4-backend.service team4-worker.service)

cd "$APP_DIR"
PREVIOUS="$(git rev-parse HEAD)"
echo "previous: ${PREVIOUS:0:7}  target: ${SHA:0:7}"

git fetch -q "$BUNDLE" "+$SHA:refs/heads/deploy/incoming" 2>/dev/null || git fetch -q "$BUNDLE"
git cat-file -e "$SHA^{commit}"

install_and_restart() {
  local rev="$1"
  git checkout -q --detach "$rev"
  if ! git diff --quiet "$PREVIOUS" "$rev" -- requirements.lock pyproject.toml; then
    .venv/bin/python -m pip install -q -r requirements.lock
  fi
  .venv/bin/python -m pip install -q --no-deps -e .
  # Development profile uses SQLite created on first deploy; MySQL needs the
  # reviewed SQL migrations applied by an operator (see migrations/README.md).
  bash -c 'set -a && source .env && set +a && .venv/bin/python - <<EOF
import os
from sqlalchemy import inspect
from app.db import Database
url = os.environ["DATABASE_URL"]
if url.startswith("sqlite:"):
    db = Database(url)
    if not inspect(db.engine).get_table_names():
        db.create_schema()
        print("schema created")
EOF'
  sudo systemctl restart "${SERVICES[@]}"
}

healthy() {
  for _ in $(seq 1 20); do
    if curl -fsS -m 3 "$HEALTH_URL" >/dev/null 2>&1 && systemctl is-active --quiet "${SERVICES[@]}"; then
      return 0
    fi
    sleep 3
  done
  return 1
}

install_and_restart "$SHA"
if healthy; then
  echo "deployed ${SHA:0:7}"
  rm -f "$BUNDLE"
  exit 0
fi

echo "health check failed; rolling back to ${PREVIOUS:0:7}" >&2
sudo journalctl -u team4-backend.service -n 30 --no-pager >&2 || true
install_and_restart "$PREVIOUS"
healthy && echo "rolled back to ${PREVIOUS:0:7}" >&2
rm -f "$BUNDLE"
exit 1
