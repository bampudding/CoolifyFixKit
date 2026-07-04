#!/bin/bash

# SSL Healer Service for Coolify (Caddy)
# - 5초 주기 모니터링, 5회 연속 실패 시 자가 진단 및 복구
# - Caddy API (/config/, /load) 기반으로 하드코딩된 경로 의존성 제거
# - SIGTERM graceful shutdown 지원
# - 복구 쿨다운 (1시간) 도입으로 무한 재시도 방지

CONTAINER_NAME="coolify-proxy"
CADDY_DATA_DIR="/data/coolify/proxy/caddy/data/caddy"
MAX_FAILURES=5
CHECK_INTERVAL=5
COOLDOWN_PERIOD=3600   # 1시간

declare -A FAIL_COUNTS
declare -A HEALED_AT

RUNNING=true
trap 'log "SIGTERM/SIGINT received. Shutting down..."; RUNNING=false' TERM INT

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1"
}

ssl_ok() {
    local domain=$1
    local timeout=${2:-5}
    curl -s -o /dev/null --connect-timeout 3 --max-time "$timeout" "https://${domain}"
}

# Caddy 관리 도메인 목록 추출 (Docker 라벨)
get_domains() {
    docker inspect $(docker ps -q) --format '{{json .Config.Labels}}' 2>/dev/null | \
    jq -r 'select(. != null) | to_entries[] | select(.key | startswith("caddy")) | .value' 2>/dev/null | \
    sed -n 's|^https\?://||p' | sort -u
}

# Caddy가 해당 domain을 알고 있는지 Caddy API로 확인
has_caddy_route() {
    local domain=$1
    local config
    config=$(docker exec "$CONTAINER_NAME" curl -s http://localhost:2019/config/ 2>/dev/null)
    if [ $? -ne 0 ]; then
        log "⚠️ Caddy API 접근 불가 (컨테이너 중지?): $domain"
        return 2  # unknown
    fi
    echo "$config" | grep -F "\"${domain}\"" >/dev/null
}

# Caddy 설정 리로드 (여러 config 경로 순차 시도, 모두 실패 시 1회 재시도)
reload_caddy() {
    log "Caddy 리로드 시도..."
    docker exec "$CONTAINER_NAME" sh -c \
        'for f in /dynamic/Caddyfile /etc/caddy/Caddyfile /data/caddy/Caddyfile /app/Caddyfile; do
            [ -f "$f" ] && caddy reload --config "$f" 2>/dev/null && exit 0
        done
        # config 파일 자동 감지 실패 시 in-memory reload 시도
        curl -X POST http://localhost:2019/load 2>/dev/null' 2>/dev/null
    local rc=$?
    sleep 3
    return $rc
}

heal_domain() {
    local domain=$1
    local now
    now=$(date +%s)

    # 쿨다운 체크
    local last_heal=${HEALED_AT["$domain"]:-0}
    if [ "$last_heal" -ne 0 ] && [ $((now - last_heal)) -lt "$COOLDOWN_PERIOD" ]; then
        local remaining=$((COOLDOWN_PERIOD - (now - last_heal)))
        log "ℹ️ [HEAL] '${domain}' 쿨다운 중 (${remaining}초 남음). 복구 스킵."
        FAIL_COUNTS["$domain"]=0
        return
    fi

    log "🚨 [HEAL] '${domain}' SSL 오류 연속 감지 (${MAX_FAILURES}회). 복구 시작..."
    HEALED_AT["$domain"]=$now

    # 1. DNS 확인
    local ip
    ip=$(dig +short "$domain" | tail -n1)
    if [ -z "$ip" ]; then
        log "⚠️ [HEAL] '${domain}' DNS 미조회. DNS 전파 상태 확인 필요."
        FAIL_COUNTS["$domain"]=0
        return
    fi
    log "ℹ️ [HEAL] '${domain}' → $ip"

    # 2. Caddy Config 라우팅 검증
    has_caddy_route "$domain"
    local route_status=$?
    if [ "$route_status" -eq 2 ]; then
        log "⚠️ [HEAL] Caddy API 접근 불가. 컨테이너 재시작으로 우회 시도."
        docker restart "$CONTAINER_NAME"
        sleep 5
        if ssl_ok "$domain" 5; then
            log "✅ [HEAL] Caddy 재시작 후 '${domain}' SSL 복구 완료."
            HEALED_AT["$domain"]=0
            FAIL_COUNTS["$domain"]=0
            return
        fi
        log "❌ [HEAL] Caddy 재시작 후에도 '${domain}' SSL 오류 지속."
        FAIL_COUNTS["$domain"]=0
        return
    fi

    if [ "$route_status" -ne 0 ]; then
        log "⚠️ [HEAL] Caddy Config에 '${domain}' 라우팅 누락. 리로드 시도."
        reload_caddy
    fi

    # 3. SSL 재검사
    if ssl_ok "$domain" 5; then
        log "✅ [HEAL] Caddy Config 리로드 후 '${domain}' SSL 복구 완료."
        HEALED_AT["$domain"]=0
        FAIL_COUNTS["$domain"]=0
        return
    fi

    # 4. 인증서 캐시 오류 의심 → 백업 및 제거
    local cert_dir="${CADDY_DATA_DIR}/certificates/acme-v02.api.letsencrypt.org-directory/${domain}"
    if [ -d "$cert_dir" ]; then
        log "⚠️ [HEAL] 인증서 캐시 백업 후 제거: ${domain}"
        mv "$cert_dir" "${cert_dir}_backup_$(date +%s)"
    fi

    # 5. Caddy 컨테이너 재시작
    log "⚡ [HEAL] Caddy Proxy 컨테이너 재시작..."
    docker restart "$CONTAINER_NAME"
    sleep 5

    # 6. 최종 복구 확인
    if ssl_ok "$domain" 5; then
        log "🎉 [HEAL] '${domain}' SSL 완전 복구 완료."
        HEALED_AT["$domain"]=0
    else
        log "❌ [HEAL] '${domain}' 자동 복구 실패. 수동 점검 필요."
    fi
    FAIL_COUNTS["$domain"]=0
}

#----------------------------------------------------------------------
log "🚀 SSL Healer 모니터링 시작 (주기: ${CHECK_INTERVAL}s, 임계치: ${MAX_FAILURES}회, 쿨다운: ${COOLDOWN_PERIOD}s)"

while $RUNNING; do
    domains=$(get_domains)

    for domain in $domains; do
        [[ "$domain" == *"sslip.io"* ]] && continue

        if ssl_ok "$domain"; then
            if [ "${FAIL_COUNTS["$domain"]:-0}" -ne 0 ] || [ "${HEALED_AT["$domain"]:-0}" -ne 0 ]; then
                log "✅ '${domain}' SSL 정상 복귀."
            fi
            FAIL_COUNTS["$domain"]=0
            HEALED_AT["$domain"]=0
        else
            # 이미 쿨다운 중인 도메인은 카운트만 증가 X
            now=$(date +%s)
            last_heal=${HEALED_AT["$domain"]:-0}
            if [ "$last_heal" -ne 0 ] && [ $((now - last_heal)) -lt "$COOLDOWN_PERIOD" ]; then
                continue
            fi

            current_fail=${FAIL_COUNTS["$domain"]:-0}
            current_fail=$((current_fail + 1))
            FAIL_COUNTS["$domain"]=$current_fail
            log "⚠️ '${domain}' SSL 오류 (연속 ${current_fail}/${MAX_FAILURES})"

            if [ "$current_fail" -ge "$MAX_FAILURES" ]; then
                heal_domain "$domain"
            fi
        fi
    done

    sleep "$CHECK_INTERVAL"
done

log "⏹️  SSL Healer 종료됨."
