#!/bin/sh
# Everything that has to be true before Caddy asks for a certificate.
#
#   ./docker/preflight.sh floodline.example.com
#
# Let's Encrypt issues a certificate by connecting *to this machine* from the outside,
# on the name you claim. Nothing in the compose stack can make that work, and when it
# fails it fails against a rate limit - five failed authorisations per hostname per
# hour - so a misconfigured first attempt locks the name out for a while and leaves no
# certificate behind.
#
# These checks run locally and answer the questions that are answerable locally. The
# one that is not - whether the internet can reach port 80 from outside - is left to a
# rehearsal against the staging CA, which has the same failure modes and no meaningful
# limit. `ACME_STAGING=1` in .env does that.
set -eu

DOMAIN="${1:-}"
if [ -z "$DOMAIN" ]; then
  echo "usage: $0 <domain>" >&2
  exit 2
fi

failures=0
pass() { printf '  ok    %s\n' "$1"; }
warn() { printf '  note  %s\n' "$1"; }
fail() { printf '  FAIL  %s\n' "$1" >&2; failures=$((failures + 1)); }

printf 'Preflight for %s\n\n' "$DOMAIN"

# --- does the name exist, and where does it point? ---------------------------
resolved=$(dig +short A "$DOMAIN" 2>/dev/null | grep -E '^[0-9.]+$' | head -1 || true)
if [ -z "$resolved" ]; then
  fail "$DOMAIN has no A record yet. Add one pointing at this machine's public address."
else
  pass "$DOMAIN resolves to $resolved"
fi

# --- is that us? -------------------------------------------------------------
public=""
for service in "https://api.ipify.org" "https://ifconfig.me/ip" "https://icanhazip.com"; do
  public=$(curl -s -m 8 "$service" 2>/dev/null | tr -d '[:space:]' || true)
  case "$public" in [0-9]*.[0-9]*) break ;; *) public="" ;; esac
done

if [ -z "$public" ]; then
  warn "could not determine this machine's public address; check the A record by hand"
elif [ -z "$resolved" ]; then
  warn "this machine's public address is $public"
elif [ "$public" = "$resolved" ]; then
  pass "that is this machine ($public)"
else
  fail "$DOMAIN points at $resolved but this machine is $public"
  # Worth naming, because it is the usual reason a home deployment cannot be reached
  # at all and no amount of port forwarding will fix it.
  case "$public" in
    10.*|172.1[6-9].*|172.2[0-9].*|172.3[01].*|192.168.*|100.6[4-9].*|100.[7-9][0-9].*|100.1[0-1][0-9].*|100.12[0-7].*)
      warn "that address is itself private - this connection is behind carrier-grade NAT,"
      warn "so inbound connections are not possible at all. A tunnel (Cloudflare Tunnel,"
      warn "Tailscale Funnel) is the way in, not port forwarding."
      ;;
  esac
fi

# --- are the ports free on this machine? -------------------------------------
# Caddy cannot bind what something else already holds, and the failure surfaces as a
# container that will not start rather than as a certificate problem.
for port in 80 443; do
  holder=$(lsof -nP -iTCP:"$port" -sTCP:LISTEN 2>/dev/null | awk 'NR==2 {print $1}' || true)
  if [ -z "$holder" ]; then
    pass "port $port is free on this machine"
  elif echo "$holder" | grep -qi "docker\|com.docke"; then
    pass "port $port is held by Docker (the proxy is probably already running)"
  else
    fail "port $port is held by $holder; Caddy cannot bind it"
  fi
done

# --- is the .env consistent with what was asked for? -------------------------
if [ -f .env ]; then
  configured=$(grep -E '^PUBLIC_DOMAIN=' .env 2>/dev/null | cut -d= -f2- || true)
  if [ -z "$configured" ]; then
    fail "PUBLIC_DOMAIN is not set in .env; the proxy would serve localhost"
  elif [ "$configured" = "$DOMAIN" ]; then
    pass "PUBLIC_DOMAIN in .env matches"
  else
    fail "PUBLIC_DOMAIN in .env is '$configured', not '$DOMAIN'"
  fi
  grep -qE '^ACME_EMAIL=.+' .env 2>/dev/null \
    && pass "ACME_EMAIL is set, so expiry warnings have somewhere to go" \
    || warn "ACME_EMAIL is empty; renewal failures will warn nobody"
  grep -qE '^ACME_STAGING=1' .env 2>/dev/null \
    && warn "ACME_STAGING=1: certificates will be untrusted rehearsal ones. Unset it once this passes." \
    || warn "ACME_STAGING is not set, so the first attempt spends the production rate limit."
else
  fail "no .env here; copy .env.example and set POSTGRES_PASSWORD and PUBLIC_DOMAIN"
fi

printf '\n'
if [ "$failures" -ne 0 ]; then
  printf '%d check(s) failed. Fix these before starting the proxy.\n' "$failures" >&2
  exit 1
fi
printf 'Local checks pass. Rehearse with ACME_STAGING=1 before going live.\n'
