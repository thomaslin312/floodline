#!/bin/sh
# Migrate, then serve.
#
# Nothing applied migrations before this: alembic.ini was in the image, the versions
# shipped inside it, and the tables were never created anywhere. A step that only a
# person can remember to run is not part of the deployment.
#
# `alembic upgrade head` is idempotent, so a restart, a redeploy and a second replica
# each cost one query against alembic_version. Concurrent starts are safe because
# PostgreSQL takes a transactional DDL lock: one wins, the others find themselves
# already at head.
#
# A failure here does not stop the server. The model degrades honestly without a
# database - the structure intersection falls back to the in-process predicate and
# says so in the run's gaps - and refusing to boot would turn a degraded map into no
# map at all. `/ready` compares the schema's revision against the one this build
# expects and answers 503 until they match, so an orchestrator holds traffic back
# without anyone losing the page.
set -eu

python -c 'import sys; from floodline.db.migrate import upgrade_to_head; print("floodline: " + upgrade_to_head(), file=sys.stderr)'

# --proxy-headers with --forwarded-allow-ips is what makes the per-client rate limit
# per *client* once Caddy is in front. Without it every visitor arrives as the proxy's
# address, the 30/minute budget becomes one bucket shared by everyone, and one caller
# can spend the whole of it against USGS on everybody else's behalf.
#
# Trusting every source is safe only because this port is not reachable from outside
# the compose network - the host publishes it on loopback and nothing else - so the
# only thing that can set the header is the proxy.
exec uvicorn floodline.api.asgi:app \
  --host 0.0.0.0 --port 8000 --workers 1 \
  --proxy-headers --forwarded-allow-ips '*'
