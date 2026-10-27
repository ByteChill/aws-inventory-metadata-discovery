#!/usr/bin/env bash
# Generate offline SQL for a range of Alembic revisions, without a database connection.
#   deploy/ddl.sh <from_rev|base> <to_rev|head>
# Writes dist/ddl/<from>_<to>.upgrade.sql and dist/ddl/<from>_<to>.downgrade.sql.
set -euo pipefail

from="${1:?usage: deploy/ddl.sh <from_rev|base> <to_rev|head>}"
to="${2:?usage: deploy/ddl.sh <from_rev|base> <to_rev|head>}"
root="$(cd "$(dirname "$0")/.." && pwd)"
out="$root/dist/ddl"
mkdir -p "$out"
cd "$root"

# offline mode only needs the dialect; nothing connects to this URL
url="postgresql+psycopg://offline/inventory"
if [ "$to" = "head" ]; then
  to="$(uv run alembic heads 2>/dev/null | awk '{print $1}')"
fi
name="${from}_${to}"

uv run alembic -x "url=$url" upgrade "${from}:${to}" --sql >"$out/$name.upgrade.sql"
uv run alembic -x "url=$url" downgrade "${to}:${from}" --sql >"$out/$name.downgrade.sql"
echo "$out/$name.upgrade.sql"
echo "$out/$name.downgrade.sql"
