#!/bin/sh
# What the deployment has to do before anyone looks at it, asserted against a running
# stack. `docker compose up -d` first; this makes no changes and exits non-zero on the
# first failure.
#
# Every check here is hermetic: nothing calls USGS, USACE or NSI. That is deliberate.
# Upstream degradation is this project's most common failure - the elevation API, the
# boundary service, the object store and FEMA's endpoint were each unreachable or
# rate-limiting at some point in one week - and a gate that goes red because an agency
# is having an afternoon gets ignored. NSI is also a .mil host that many CI runners
# cannot resolve. The scheduled `upstream` workflow covers the live path, where red
# means "an agency moved" rather than "this commit is bad".
#
# So this proves the deployment is wired, not that the model is right: the routes exist
# and validate, the schema is applied, the volume is writable by the unprivileged user.
# The model's own correctness is what the 801 tests are for.
set -eu

BASE="${1:-http://127.0.0.1:8000}"
COMPOSE="${COMPOSE:-docker compose}"
PROXY="${PROXY:-https://localhost}"
PROXY_PLAIN="${PROXY_PLAIN:-http://localhost}"
failures=0

pass() { printf '  ok    %s\n' "$1"; }
fail() { printf '  FAIL  %s\n' "$1" >&2; failures=$((failures + 1)); }

# Truncate the body first. curl leaves the previous file in place when it cannot
# connect, so without this a failing check quotes the last response that worked - the
# /health failure above reported the map's HTML, which is a misleading gate.
body() { head -c 160 /tmp/smoke.body | tr '\n' ' '; }

# --- the probes an orchestrator uses -----------------------------------------
: >/tmp/smoke.body
code=$(curl -s -o /tmp/smoke.body -w '%{http_code}' -m 15 "$BASE/health" || true)
if [ "$code" = "200" ] && grep -q '"ok":true' /tmp/smoke.body; then
  pass "GET /health is 200"
else
  fail "GET /health returned $code: $(body)"
fi

: >/tmp/smoke.body
code=$(curl -s -o /tmp/smoke.body -w '%{http_code}' -m 30 "$BASE/ready" || true)
if [ "$code" = "200" ]; then
  pass "GET /ready is 200"
else
  fail "GET /ready returned $code: $(body)"
fi

# Named separately from the status code: a 200 with a failed check would mean the
# endpoint had stopped reporting what it is for.
python3 - <<'PY' || failures=$((failures + 1))
import json, sys
try:
    body = json.load(open("/tmp/smoke.body"))
except (OSError, ValueError) as exc:
    print(f"  FAIL  /ready did not return JSON: {exc}", file=sys.stderr)
    sys.exit(1)
checks = body.get("checks", {})
for name in ("database", "schema", "terrain_store"):
    if name not in checks:
        print(f"  FAIL  /ready has no {name} check", file=sys.stderr); sys.exit(1)
    if not checks[name]["ok"]:
        print(f"  FAIL  /ready {name}: {checks[name]['detail']}", file=sys.stderr); sys.exit(1)
print("  ok    /ready reports database, schema and terrain_store healthy")
if not checks["schema"]["detail"].startswith("at "):
    print(f"  FAIL  schema not at a revision: {checks['schema']['detail']}", file=sys.stderr)
    sys.exit(1)
print(f"  ok    schema {checks['schema']['detail']}")
PY

# --- the map, which the image once did not serve at all ----------------------
# This is the regression that survived a passing test suite: the container ran the API
# factory, so `GET /` was a 404 while `floodline serve` was fine.
: >/tmp/smoke.body
code=$(curl -s -o /tmp/smoke.body -w '%{http_code}' -m 15 "$BASE/" || true)
if [ "$code" = "200" ] && grep -q "<title>floodline</title>" /tmp/smoke.body; then
  pass "GET / serves the map ($(wc -c < /tmp/smoke.body | tr -d ' ') bytes)"
else
  fail "GET / returned $code without the map"
fi

for path in /methodology /api/docs; do
  code=$(curl -s -o /dev/null -w '%{http_code}' -m 15 "$BASE$path" || true)
  [ "$code" = "200" ] && pass "GET $path is 200" || fail "GET $path returned $code"
done

# --- the service, without touching upstream ----------------------------------
# A rejected payload proves the route is mounted where it should be and that the
# schema runs, and it costs no DEM fetch. A valid one would reach USGS.
code=$(curl -s -o /dev/null -w '%{http_code}' -m 15 -X POST "$BASE/api/scenario" \
  -H 'content-type: application/json' -d '{"huc":"abc","discharge_cms":-1}' || true)
if [ "$code" = "422" ]; then
  pass "POST /api/scenario rejects a bad payload with 422"
else
  fail "POST /api/scenario returned $code, expected 422 (404 means the route moved)"
fi

# --- the schema itself, not just the version row -----------------------------
# `alembic_version` says a migration ran. It does not say the migration created what
# the queries need, and a missing GiST index is a sequential scan that still returns
# the right answer, slowly and silently.
if $COMPOSE exec -T db sh -c \
    'psql -U $POSTGRES_USER -d $POSTGRES_DB -tAc "select indexname from pg_indexes where tablename = '"'"'buildings'"'"'"' \
    2>/dev/null | grep -q ix_buildings_geom; then
  pass "buildings has its GiST index"
else
  fail "buildings is missing ix_buildings_geom"
fi

# --- the volume, under the unprivileged user ---------------------------------
if $COMPOSE exec -T api sh -c \
    'touch /var/lib/floodline/.smoke && rm /var/lib/floodline/.smoke' >/dev/null 2>&1; then
  pass "the data volume is writable by uid 10001"
else
  fail "the data volume is not writable by the container's user"
fi

# --- the proxy, if this stack has one ----------------------------------------
# `-k` because a local stack has no domain, so Caddy serves localhost from its own
# internal CA. On a real deployment the certificate is from Let's Encrypt and this
# would pass without it.
if [ "${SMOKE_PROXY:-1}" = "1" ]; then
  code=$(curl -sk -o /dev/null -w '%{http_code}' -m 20 "$PROXY/health" || true)
  if [ "$code" = "200" ]; then
    pass "the proxy serves HTTPS"
  else
    fail "GET $PROXY/health returned $code"
  fi

  # Caddy redirects rather than serving plaintext. A deployment that quietly answered
  # on http would send the whole map, and every request to it, in the clear.
  code=$(curl -s -o /dev/null -w '%{http_code}' -m 20 "$PROXY_PLAIN/" || true)
  if [ "$code" = "308" ] || [ "$code" = "301" ] || [ "$code" = "302" ]; then
    pass "plain HTTP redirects to HTTPS ($code)"
  else
    fail "GET $PROXY_PLAIN/ returned $code, expected a redirect to HTTPS"
  fi

  # The app must not be reachable except through the proxy and on loopback. This is
  # what makes --forwarded-allow-ips '*' safe.
  bindings=$($COMPOSE ps --format json api 2>/dev/null | tr ',' '\n' | grep -c '0.0.0.0:8000' || true)
  if [ "${bindings:-0}" = "0" ]; then
    pass "the application is not published on every interface"
  else
    fail "the application is published on 0.0.0.0; only the proxy should be"
  fi
fi

printf '\n'
if [ "$failures" -ne 0 ]; then
  printf '%d check(s) failed\n' "$failures" >&2
  exit 1
fi
printf 'all checks passed\n'
