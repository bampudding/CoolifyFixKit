#!/bin/bash

# SSL Healer Service for Coolify (Caddy)
# 5초 주기 모니터링, 5회 연속 실패 시 자가 진단 및 복구

CONTAINER_NAME="coolify-proxy"
CADDY_DATA_DIR="/data/coolify/proxy/caddy/data/caddy"
MAX_FAILURES=5
CHECK_INTERVAL=5

# 실패 횟수 기록용 연상 배열
declare -A FAIL_COUNTS
# 복구 작업 시도 완료 여부 기록 (무한 반복 방지)
declare -A HEALED_STATUS

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1"
}

# Caddy가 관리 중인 도메인 목록 추출 (Docker 라벨 스캔)
get_domains() {
    docker inspect $(docker ps -q) --format '{{json .Config.Labels}}' 2>/dev/null | \
    jq -r 'select(. != null) | to_entries[] | select(.key | startswith("caddy")) | .value' 2>/dev/null | \
    grep -E '^https?://' | sed -E 's|https?://||' | sort -u
}

heal_domain() {
    local domain=$1
    
    # 이미 복구 작업을 실행했으나 여전히 실패한 경우, 중복 실행 차단하고 정지
    if [ "${HEALED_STATUS["$domain"]:-0}" -eq 1 ]; then
        log "ℹ️ [HEAL] '${domain}'은(는) 이미 복구 시도를 완료했으나 복구되지 않았습니다. 추가 작업 없이 냅둡니다."
        FAIL_COUNTS["$domain"]=0 # 실패 횟수를 리셋하여 다음 모니터링에서 반복 복구 실행을 방지
        return
    fi

    log "🚨 [HEAL] '${domain}' 도메인 SSL 핸드셰이크 오류 연속 감지 (${MAX_FAILURES}회). 복구 시작..."

    # 1. DNS 확인
    local ip
    ip=$(dig +short "$domain" | tail -n1)
    if [ -z "$ip" ]; then
        log "⚠️ [HEAL] '${domain}' DNS가 조회되지 않습니다. DNS 전파 상태를 확인하세요."
        HEALED_STATUS["$domain"]=1
        FAIL_COUNTS["$domain"]=0
        return
    fi
    log "ℹ️ [HEAL] '${domain}' IP: $ip"

    # 2. Caddy Config 검증 (Caddy API를 통해 라우팅 등록 여부 조회)
    local has_route
    has_route=$(docker exec "$CONTAINER_NAME" curl -s http://localhost:2019/config/ | grep -F "$domain")
    if [ -z "$has_route" ]; then
        log "⚠️ [HEAL] Caddy Config에 '${domain}' 라우팅 정보가 누락되었습니다. Caddy 로드를 강제 요청합니다."
        docker exec "$CONTAINER_NAME" caddy reload --config /dynamic/Caddyfile 2>/dev/null
        sleep 3
    fi

    # 3. SSL 핸드셰이크 재검사
    if curl -Iv "https://${domain}" --connect-timeout 3 >/dev/null 2>&1; then
        log "✅ [HEAL] Caddy Config 리로드 후 '${domain}' SSL이 복구되었습니다."
        FAIL_COUNTS["$domain"]=0
        HEALED_STATUS["$domain"]=0
        return
    fi

    # 4. 인증서 캐시 오류/만료 의심 점검 및 캐시 초기화
    local cert_dir="${CADDY_DATA_DIR}/certificates/acme-v02.api.letsencrypt.org-directory/${domain}"
    if [ -d "$cert_dir" ]; then
        log "⚠️ [HEAL] 인증서 캐시 오류가 의심됩니다. '${domain}'의 기존 인증서 캐시를 백업하고 만료 처리합니다."
        mv "$cert_dir" "${cert_dir}_backup_$(date +%s)"
    fi

    # 5. Caddy Proxy 서비스 재시작 (Docker socket 리셋)
    log "⚡ [HEAL] Caddy Proxy (${CONTAINER_NAME}) 컨테이너 재시작을 수행합니다."
    docker restart "$CONTAINER_NAME"
    
    # 기동 대기
    sleep 5
    
    # 복구 작업을 시도한 것으로 기록
    HEALED_STATUS["$domain"]=1
    FAIL_COUNTS["$domain"]=0 # 무한 반복 시도 방지를 위해 실패 카운터 클리어

    # 6. 최종 복구 결과 검증
    if curl -Iv "https://${domain}" --connect-timeout 5 >/dev/null 2>&1; then
        log "🎉 [HEAL] 프록시 엔진 정상화 및 SSL 인증서 재발급이 완료되어 '${domain}'이 복구되었습니다."
        HEALED_STATUS["$domain"]=0
    else
        log "❌ [HEAL] '${domain}' 자동 복구 시도를 1회 완료했으나 여전히 실패 상태입니다. 추가 작업을 중지하고 냅둡니다."
    fi
}

log "🚀 SSL Healer 모니터링 서비스를 시작합니다. (체크 주기: ${CHECK_INTERVAL}초, 임계치: ${MAX_FAILURES}회)"

while true; do
    domains=$(get_domains)
    
    for domain in $domains; do
        # sslip.io 등 자동 도메인이나 서브 도메인 패턴 매칭 체크
        if [[ "$domain" == *"sslip.io"* ]]; then
            continue
        fi

        # SSL handshake 체크
        if curl -Iv "https://${domain}" --connect-timeout 2 >/dev/null 2>&1; then
            if [ "${FAIL_COUNTS["$domain"]:-0}" -ne 0 ] || [ "${HEALED_STATUS["$domain"]:-0}" -eq 1 ]; then
                log "✅ '${domain}' SSL 상태 정상 복귀."
            fi
            FAIL_COUNTS["$domain"]=0
            HEALED_STATUS["$domain"]=0
        else
            # 이미 복구 작업을 수행했으나 실패 상태가 복구되지 않은 도메인은 추가 복구 로직을 수행하지 않고 패스
            if [ "${HEALED_STATUS["$domain"]:-0}" -eq 1 ]; then
                continue
            fi

            current_fail=${FAIL_COUNTS["$domain"]:-0}
            current_fail=$((current_fail + 1))
            FAIL_COUNTS["$domain"]=$current_fail
            log "⚠️ '${domain}' SSL 오류 감지 (연속 ${current_fail}/${MAX_FAILURES}회)"
            
            if [ "$current_fail" -ge "$MAX_FAILURES" ]; then
                heal_domain "$domain"
            fi
        fi
    done
    
    sleep "$CHECK_INTERVAL"
done
