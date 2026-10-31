#!/bin/bash
# TLS observations must not turn resource/DNS/upstream failures into certificate loss.
set -uo pipefail

CONTAINER_NAME=${CONTAINER_NAME:-coolify-proxy}
MAX_FAILURES=${MAX_FAILURES:-5}
CHECK_INTERVAL=${CHECK_INTERVAL:-5}
COOLDOWN_PERIOD=${COOLDOWN_PERIOD:-3600}
STATE_DIR=${GUARD_STATE:-/state}
SSL_APPLY=${SSL_APPLY:-false}
SSL_CRITICAL_RESTART=${SSL_CRITICAL_RESTART:-false}
declare -A FAIL_COUNTS
RUNNING=true

log() { echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*"; }
trap 'RUNNING=false' TERM INT

docker_cmd() { timeout 15 docker "$@"; }

ssl_ok() {
    # HTTP errors are app failures, not TLS failures. curl without --fail accepts them.
    curl -s -o /dev/null --connect-timeout 3 --max-time 5 "https://$1"
}

get_domains() {
    local ids
    ids=$(docker_cmd ps -q) || return 1
    [[ -n "$ids" ]] || return 0
    # IDs originate from Docker, never from a domain label.
    # shellcheck disable=SC2086
    docker_cmd inspect $ids --format '{{json .Config.Labels}}' 2>/dev/null |
        jq -r 'select(. != null) | to_entries[] |
            select(.key | test("^caddy(_[0-9]+)?$")) | .value' |
        tr ', ' '\n\n' | sed -E '/^http:\/\//d; s#^https://##; s#[:/].*$##' | sort -u
}

has_caddy_route() {
    local config
    # The deployed caddy image has wget; curl is not part of its contract.
    config=$(docker_cmd exec "$CONTAINER_NAME" wget -qO- -T 5 \
        http://127.0.0.1:2019/config/ 2>/dev/null) || return 2
    jq -e 'type == "object"' >/dev/null 2>&1 <<<"$config" || return 2
    jq -e --arg domain "$1" \
        '[.. | objects | .host? // empty | .[]?] | index($domain) != null' \
        >/dev/null 2>&1 <<<"$config"
}

heal_domain() {
    local domain=$1 now last=0 status
    now=$(date +%s)
    [[ ! -f "$STATE_DIR/ssl-restart-at" ]] || read -r last < "$STATE_DIR/ssl-restart-at"
    [[ "$last" =~ ^[0-9]+$ ]] || { log 'Invalid SSL cooldown state; recovery blocked'; return; }
    # A single proxy serves every domain: the cooldown must be global and durable.
    (( now - last >= COOLDOWN_PERIOD )) || return 0
    if [[ "$SSL_APPLY" != true ]]; then
        log "TLS check failed repeatedly for $domain; observation mode"
        return
    fi
    if [[ "$SSL_CRITICAL_RESTART" != true ]]; then
        log "Critical proxy restart disabled; manual diagnosis required for $domain"
        return
    fi
    if ! timeout 8 python3 /app/resource_guard.py check >/dev/null; then
        log "Resource pressure or missing telemetry; proxy recovery deferred for $domain"
        return
    fi
    has_caddy_route "$domain"
    status=$?
    if (( status == 0 )); then
        log "Caddy admin and route respond for $domain; check DNS, network and upstream"
        return
    elif (( status == 1 )); then
        log "Caddy route missing for $domain; reconcile the resource in Coolify"
        return
    fi
    # Only repeated TLS failures AND an unavailable admin API can request restart.
    # Never move/delete certificates or load a guessed/static Caddyfile.
    mkdir -p "$STATE_DIR" || return
    printf '%s\n' "$now" > "$STATE_DIR/ssl-restart-at.tmp" || return
    mv "$STATE_DIR/ssl-restart-at.tmp" "$STATE_DIR/ssl-restart-at" || return
    log "Caddy admin unavailable; requesting one bounded proxy restart ($domain)"
    timeout 30 docker restart --time 10 "$CONTAINER_NAME" || \
        log 'Proxy restart failed or timed out; cooldown retained'
}

main() {
    local domains domain count
    for count in "$MAX_FAILURES" "$CHECK_INTERVAL" "$COOLDOWN_PERIOD"; do
        [[ "$count" =~ ^[1-9][0-9]*$ ]] || { log 'Invalid SSL monitor interval'; return 1; }
    done
    log "TLS monitor started (apply=$SSL_APPLY)"
    while $RUNNING; do
        domains=$(get_domains) || { log 'Docker inventory unavailable'; domains=; }
        for domain in $domains; do
            $RUNNING || break
            [[ "$domain" =~ ^[a-zA-Z0-9]([a-zA-Z0-9.-]*[a-zA-Z0-9])?$ ]] || continue
            [[ "$domain" != *sslip.io ]] || continue
            if ssl_ok "$domain"; then
                FAIL_COUNTS["$domain"]=0
            else
                count=$((${FAIL_COUNTS["$domain"]:-0} + 1))
                FAIL_COUNTS["$domain"]=$count
                if (( count >= MAX_FAILURES )); then
                    heal_domain "$domain"
                    FAIL_COUNTS["$domain"]=0
                fi
            fi
        done
        sleep "$CHECK_INTERVAL" &
        wait $! || true
    done
    log 'TLS monitor stopped'
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then main; fi
