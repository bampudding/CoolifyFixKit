# CoolifyFixKit

Debian과 하위 Incus의 메모리 압박을 기록하고, 명시적으로 지정한 Docker 컨테이너의 OOM 종료를 복구합니다. 기존 TLS 감시도 포함합니다.

**기본값은 관찰 모드입니다.** 운영 적용에는 기존 Coolify 리소스의 재배포가 필요합니다. Docker 소켓과 선택적인 Incus 소켓은 호스트 관리자 권한에 해당합니다. 소켓을 읽기 전용으로 마운트해도 API 쓰기를 막지는 않습니다.

[2026-10-31 실측 조사 및 적용안](docs/2026-10-31_debian-oom-report.md)을 먼저 확인하세요.

## 기존 Coolify에서 배포하기

기존 `bampudding/CoolifyFixKit` 애플리케이션의 `docker-compose.yml`을 사용합니다. `coolify-ssl-healer` 서비스 이름을 유지하며 `resource-guard`가 추가됩니다. 운영 Coolify에 별도의 복제 리소스를 만들지 않습니다.

초기에는 `GUARD_APPLY=false`, `SSL_APPLY=false`로 관찰합니다. `/proc`와 `/sys/fs/cgroup`이 각각 `/host/proc`, `/host/cgroup`에 실제로 읽기 전용으로 연결됐는지 배포 결과에서 확인합니다. `sample` 로그에 실행 중인 Incus 3개가 보여야 합니다. 새 서비스의 CPU, 메모리, PID 및 로그 제한도 실제 `docker inspect` 결과에서 확인합니다.

감시 항목은 호스트 `MemAvailable`, 메모리 및 I/O PSI, 호스트 전체 OOM 카운터, 각 Incus의 메모리 사용량과 high/max, swap, OOM 이벤트, 페이지 재읽기 및 직접 회수 카운터입니다. Incus root의 이벤트는 하위 서비스도 포함합니다. 호스트의 `oom_kill`도 하위 cgroup OOM을 포함하므로 두 카운터를 합산해 별개 사건으로 세지 않습니다. 중지된 Incus는 시작하지 않습니다.

기본 관측 간격은 5초이며, 복구 대상 조회 시간은 추가될 수 있습니다. 정상 상태는 60초 간격으로 기록하고 OOM, 위험 상태 변화와 예상 고갈은 즉시 JSON으로 출력합니다. guard 로그는 최대 약 128MiB로 순환하며 보존 기간은 실제 로그 크기에 따라 달라집니다. Discord 개인 메시지는 아래의 네이티브 알림 서비스가 담당합니다.

## OOM 복구 대상 지정하기

복구할 서비스의 정규 Coolify Compose에 다음 라벨을 지정합니다. 아래 `512`는 예시이며 실제 시작 피크에 맞춰 바꿉니다. 데이터베이스와 대화형 데스크톱은 기본 대상에 넣지 않습니다.

```yaml
labels:
  io.coolify-fixkit.oom-recover: "true"
  io.coolify-fixkit.recovery-key: "my-stateless-api"
  io.coolify-fixkit.restart-memory-mib: "512"
```

검토 후 Healer 리소스의 `GUARD_APPLY=true`로 켭니다. 최근 24시간 내 `OOMKilled=true`, 종료 상태 `exited`, 코드 `137`인 대상만 기존 컨테이너 ID로 시작합니다. 실행 중인 컨테이너와 교체 배포가 있으면 시작하지 않습니다. 같은 복구 키는 모든 배포에 유지하세요. Coolify application/Compose 소유자가 같은 교체 컨테이너도 확인합니다.

호스트 여유 4GiB와 선언한 시작 메모리가 확보되고, 메모리 PSI some과 I/O PSI full의 avg10이 각각 10% 미만이어야 합니다. Incus 내부 대상은 해당 Incus의 압박과 soft/hard 한도 여유도 확인합니다. 호스트 상태가 연속 3회 안정된 뒤 30초 주기로 대상을 확인하고, 시작 직전에 다시 읽습니다.

동일 대상의 복구는 시간당 한 번, 하루 세 번까지입니다. 전체 복구도 시간당 세 번으로 제한합니다. 실행 전에 상태 볼륨에 시도를 저장하므로 실패나 시간 초과도 제한에 포함됩니다. Healer 재시작 후에도 같은 OOM 사건을 반복 처리하지 않습니다. 시작 요청 성공과 애플리케이션의 건강 상태는 별개이며 후자는 기존 Docker/Coolify healthcheck가 확인합니다.

하위 Incus의 Docker까지 복구하려면 `compose.incus.yml`의 명시적 소켓 마운트와 `INCUS_ENABLED=true` 설정을 기존 리소스에 합칩니다. 관찰 자체에는 Incus 소켓이 필요 없습니다. `incus exec`는 고정 Docker argv만 사용하고 인스턴스 전체를 재시작하지 않습니다.

## 새 빌드의 실행 전 확인하기

Debian에 체크아웃한 이 저장소에서 실행합니다.

```sh
python3 resource_guard.py check --proc /proc --cgroups /sys/fs/cgroup --required-mib 4096
```

종료 코드 0이면 관측 시점의 조건을 통과한 것입니다. 1이면 자원 부족, 압박 또는 측정 실패입니다. 실제 빌드 진입점이 이 종료 코드를 따르도록 연결해야 예방 기능이 됩니다. 이 명령은 자원을 예약하거나 여러 빌드의 동시 진입을 직렬화하지 않습니다. 동시 빌드 슬롯/잠금과 함께 사용하세요.

`check`는 호스트의 추가 메모리 요구량과 모든 실행 중인 Incus의 압박/한도 여유를 확인합니다. 특정 빌드가 들어갈 Incus의 예상 사용량을 예약하는 스케줄러는 아닙니다.

## TLS 감시 설정하기

인증서를 이동하거나 지우지 않으며, 추측한 Caddyfile을 로드하지 않습니다. HTTP 오류는 TLS 실패로 취급하지 않습니다. 살아 있는 Caddy admin/route에 대한 외부 접속 실패는 DNS, 네트워크 또는 upstream 점검 대상으로 기록합니다.

`SSL_APPLY=true`와 별도 최후 수단 허용인 `SSL_CRITICAL_RESTART=true`를 모두 명시한 경우에만, 자원이 안정적이고 TLS 실패가 반복되며 Caddy admin도 응답하지 않을 때 프록시 재시작을 요청합니다. 라우트 누락은 Coolify에서 재조정해야 합니다. 프록시 전체에 적용하는 1시간 cooldown은 재시작 실패 후에도 유지됩니다.

| 변수 | 기본값 | 용도 |
|---|---|---|
| `GUARD_APPLY` | `false` | 라벨로 허용한 OOM 종료 컨테이너의 자동 시작 |
| `SSL_APPLY` | `false` | TLS 복구 기능 허용 |
| `SSL_CRITICAL_RESTART` | `false` | 중요 Caddy의 최후 수단 재시작을 별도로 허용 |
| `INCUS_ENABLED` | `false` | 선택적인 Incus 내부 Docker 조회/복구 |
| `HOST_RESERVE_MIB` | `4096` | 호스트에 남길 최소 메모리 |
| `GUARD_INTERVAL` | `5` | 자원 관측 간격(초) |
| `GUARD_LOG_INTERVAL` | `60` | 정상 상태 로그 간격(초), 위험 이벤트는 즉시 기록 |
| `HOST_PROC` | `/host/proc` | 실제 Debian proc 마운트 |
| `HOST_CGROUP` | `/host/cgroup` | 실제 Debian cgroup v2 마운트 |
| `GUARD_STATE` | `/state` | 영속 cooldown 및 중복 실행 잠금 |
| `MAX_FAILURES` | `5` | TLS 연속 실패 임계치 |
| `CHECK_INTERVAL` | `5` | TLS 검사 간 대기(초), 각 도메인 검사 시간은 추가됨 |
| `COOLDOWN_PERIOD` | `3600` | 프록시 재시작 간 최소 간격(초) |

## 검증하기

```sh
python3 -m unittest discover -s tests -v
bash -n check_and_heal.sh
docker compose config --quiet
docker compose -f docker-compose.yml -f compose.incus.yml config --quiet
```

`tests/integration_oom.py --run`은 Debian에서 `coolify-fixkit:audit-20261031` 이미지와 저장소 루트의 `PYTHONPATH=.`를 사용해 명시적으로 실행하는 통합 검사입니다. 새 64MiB 임시 컨테이너 하나에서 OOM을 발생시키고 복구/중복 방지를 확인한 뒤 제거합니다. 운영 컨테이너는 복구 대상으로 선택하지 않습니다. 일반 단위 테스트에서는 실행하지 않습니다.

`python3 scripts/disk_read_probe.py /dev/sda`는 관리자 권한이 필요한 읽기 전용 검사입니다. 4KiB 직접 읽기 400회, 최대 100 IOPS로 제한하며 쓰기 성능이나 최대 IOPS를 측정하지 않습니다. `scripts/analyze_sar.py`는 `sadf -j` 결과를 KST 기준 일별 CSV로 요약합니다.

## Debian 호스트의 실제 대역폭과 soft limit 유지하기

2026-10-31 후속 조치로 `host_guard.py`를 Debian의 `coolify-fixkit-host.service`로 설치했다. 이것은 기존 Coolify TLS 컨테이너와 별도로 동작하는 호스트 보조 구성요소다. 정규 소스와 정책은 이 저장소의 `host_guard.py`, `deploy/host-policy.json`, `deploy/coolify-fixkit-host.service`다. 이 보조 서비스는 Coolify 애플리케이션 배포와 독립적으로 관리한다.

설치된 Incus 7.0.1은 `128MiB,7200iops`를 거부한다. 보조 서비스는 기존 Incus IOPS 설정과 hard RAM 한도를 유지하고 `io.max`의 `rbps/wbps`만 별도로 설정한다. 3개 Incus의 soft 한도는 각각 실제 `memory.max`의 93%로 설정하며, Incus의 live update 후 한도가 초기화돼도 5초마다 복원한다. 위험 상태에서 프로세스를 강제 종료하거나 인스턴스를 재시작하지 않는다.

| 인스턴스 | 읽기 상한 | 쓰기 상한 | hard RAM | soft RAM |
|---|---:|---:|---|---|
| fedora-heart | 128MiB/s | 48MiB/s | 기존 설정 유지 | 실제 hard 한도의 93% |
| fedora-enxsis | 64MiB/s | 16MiB/s | 기존 설정 유지 | 실제 hard 한도의 93% |
| fedora-musecat | 128MiB/s | 64MiB/s | 기존 설정 유지 | 실제 hard 한도의 93% |

각각 기존 read 7,200 / write 12,600 IOPS도 유지한다. 이 정책은 `/etc/coolify-fixkit/host-policy.json`이 소유한다. 한도 변경 시 이 파일을 검토하고 서비스를 재시작한다. `raw.lxc`에 별도 memory.high를 다시 추가해 두 소유자가 경쟁하게 만들지 않는다. 이 정책은 이름을 명시한 3개 인스턴스에만 적용한다.

60초 간격 `host-io` 로그에는 guest별 I/O 누적값, RAM 한도/사용량, file refault와 직접 회수, 상위 프로세스별 실제 read/write bytes 변화가 기록된다. 명령 인자와 환경변수는 수집하지 않는다. 속도 상한은 폭주를 제한하지만 일일 누적 쓰기 감소를 보장하지 않는다. 생산 작업과 metadata CoW의 영향은 이 기록으로 구분해 후속 조정한다. [최근 로그 검토와 개선 우선순위](docs/2026-10-31_log-review.md)를 참고한다.

```sh
systemctl status coolify-fixkit-host.service
journalctl -u coolify-fixkit-host.service --since '10 minutes ago' -o cat
python3 host_guard.py --policy deploy/host-policy.json --once
```

마지막 명령은 관찰만 한다. 실제 적용에는 `--apply`가 필요하다. 현재 단일 SSD의 Btrfs root를 지원하며, 지원하지 않는 root source나 SSD가 아니면 시작에 실패한다. RAID/LVM/복수 Btrfs 장치로 변경할 때는 장치 해석을 재검토한다.

`fedora-heart`에는 `limits.memory.swap=2GiB`를 live 적용하고 오래된 raw memory.high 한 줄을 제거했다. 하드 한도 `16GB`를 `16GiB`로 임의 변경하지 않았다. 프로필에 있는 별도 raw 옵션은 보존한다.

`deploy/build-budgets.sh`와 `deploy/gradle-budget.properties`의 기본값도 `fedora-heart`에 반영했다. CMake/Cargo jobs 4, CTest 2, Gradle workers 2, daemon heap 2GiB, Kotlin daemon heap 1GiB 및 Gradle build cache다. 새 셸/빌드에 적용되며 실행 중인 빌드나 명시적인 `-j`/JVM 인자를 강제로 바꾸지는 않는다. 전체 빌드를 직렬화하는 중앙 scheduler는 아직 연결하지 않았다.

운영 rollback의 원본 Incus 구성은 Debian의 `/root/fixkit-performance-20261031/*.before.yaml`이다. helper 중지는 이미 적용한 커널 한도를 즉시 되돌리지 않는다. 복구 시 서비스를 중지한 후 `io.max`의 rbps/wbps와 heart memory.high를 이전 값으로 복원하고, swap/raw 설정은 백업과 현재 구성을 비교해 해당 항목만 되돌린다. 전체 YAML을 덮어써 다른 세션의 변경을 지우지 않는다. `/etc/zshenv.before-fixkit-20261031`은 해당 파일이 원래 있던 경우에만 생성한다.

## 중요 서비스는 최후 순위로 보호하기

`fedora-musecat` 전체, Debian `coolify-proxy`와 Incus는 운영상 중요 영역이다. 호스트 보조 서비스의 `oom_protection` 정책은 이들의 기존보다 강한 설정을 보존하면서 `oom_score_adj=-950` 이하를 유지한다. 알려진 heart 개발 compiler/Gradle/Kotlin 프로세스는 +500 이상으로 설정해 메모리 부족 시 먼저 선택되게 한다. 일반 Java 서버나 PocketBase/Caddy를 빌드로 분류하지 않는다.

musecat cgroup에는 `memory.low=4GiB`의 best-effort 회수 보호와 `user.oomd_avoid=1`을 적용한다. Incus 및 Caddy cgroup에도 oomd avoid를 적용하고 Incus에는 256MiB memory.low를 둔다. musecat 내부 system/user slice도 ManagedOOMPreference=avoid로 설정했다. `avoid`와 유한한 -950을 사용하므로 가장 나쁜 상황의 커널 복구 가능성은 남긴다. 새로 -1000 immunity를 설정하거나 OOM killer를 끄지 않는다. 기존 -1000 설정은 약화하지 않는다.

호스트 systemd-oomd는 기존 비활성 상태를 유지한다. 새 감시기가 서비스/guest/프로세스에 kill, stop, restart를 호출하지 않는다. 허용된 OOM 복구도 이미 종료된 컨테이너의 start에 한정한다. Caddy의 자동 재시작은 앞서 명시한 별도 override가 기본 비활성이다.

이 정책은 전역 OOM 우선순위를 바꾸며, musecat 자체의 hard cap 또는 내부 Docker hard cap을 없애지는 않는다. 해당 cgroup 자체가 한도에 도달하면 커널은 다른 cgroup의 개발 작업을 희생시킬 수 없다. 총 용량/실제 working set과 개별 hard cap은 계속 별도로 관리해야 한다.

Incus의 재시작 후에도 Exec OOM 우선순위를 유지하려면 `deploy/incus-oom-protection.conf`를 `/etc/systemd/system/incus.service.d/85-fixkit-oom.conf`에 둔다. 기존 프로세스는 보조 서비스가 live 보호하므로 이 파일을 적용하기 위해 Incus를 재시작하지 않는다.

## 같은 Discord 봇으로 개인 알림 받기

`discord_notify.py`는 root 전용 Unix 소켓 `/run/coolify-fixkit/notify.sock`의 `POST /events` 웹훅을 받아 기존 CoSAI Discord 봇의 REST API로 소유자에게 DM을 보낸다. Discord의 채널용 incoming webhook URL을 DM 주소로 사용하지 않는다. 별도 Gateway 로그인이나 두 번째 CoSAI 클라이언트를 실행하지 않는다.

Debian의 `/etc/coolify-fixkit/discord.json`에 `bot_token`, `recipient_id`를 저장하며 권한은 root 0600이다. 현재 값은 heart Coolify의 기존 Discord 서비스 토큰과 확인한 봇 소유자에서 가져왔다. 이 파일은 저장소나 Healer 컨테이너에 전달하지 않는다. 토큰 교체 시 이 비공개 파일도 갱신한다.

`deploy/coolify-fixkit-discord.service`는 독립 알림 서비스다. OOM 카운터 증가, 15초 이상 자원 압박, 압박 해소를 감시하고 host guard의 정책 오류/Resource Guard의 복구 이벤트도 받는다. 감시/발송 실패는 중요 서비스의 종료·재시작으로 이어지지 않는다. 정상 상태나 매 관측 주기를 DM으로 보내지 않는다.

서비스 재시작 시 런타임 디렉터리를 유지해 이미 실행 중인 resource-guard의 디렉터리 bind mount에서도 새 소켓을 볼 수 있게 한다. Unix 소켓 파일 자체는 기동할 때 다시 만든다.

동일 종류/대상은 5분 중복 제한을 적용한다. 최대 100개 영속 outbox, Discord rate limit에 따른 재시도와 nonce를 사용한다. 네트워크/Discord 오류나 DM 거부 시 알림이 지연될 수 있으며 전송 성공은 읽음 확인과 다르다.

```sh
systemctl status coolify-fixkit-discord.service
python3 /usr/local/lib/coolify-fixkit/discord_notify.py --test
journalctl -u coolify-fixkit-discord.service -o cat
```

테스트 명령은 실행 중인 웹훅으로 한 건을 보내 실제 DM 발송을 요청한다. 로컬 웹훅은 `kind`(oom/pressure/recovered/policy-error/recovery/test), 선택적 `scope`, `message`만 받으며 외부 TCP 포트는 열지 않는다. 소켓 디렉터리를 선택적으로 resource-guard에 읽기 전용 마운트하면 컨테이너 복구 이벤트도 전달할 수 있다. 소켓이 없을 때 guard는 기존 작업을 계속한다.

## 감시 효율과 자체 복구 (2026-11-04)

호스트 보조 감시기가 5초마다 자원 표본을 `/run/coolify-fixkit/metrics.json`에 원자적으로 게시한다. `/run`은 tmpfs이므로 이 표본은 SSD에 기록하지 않는다. Discord와 resource-guard는 같은 boot의 15초 이내 표본을 공유한다. 표본이 없거나 오래됐거나 잘못된 경우 각자 실제 proc/cgroup을 읽어 감시를 유지한다. 실행 전 입장 검사와 복구 시작 직전에는 공유 표본 대신 최신 값을 직접 읽는다.

resource-guard는 라벨로 허용된 종료 컨테이너가 있을 때만 전체 배포 목록을 inspect한다. 교체 배포 확인, 영속 cooldown, 시작 메모리 검사와 기존 OOM 복구 제한은 유지한다. 정상 전체 로그는 60초 주기이며 사건과 상태 변화는 즉시 기록한다.

SIGTERM/SIGINT 처리기는 락 없는 종료 플래그만 변경한다. Event.wait 도중 신호 처리기가 Event.set의 같은 mutex를 다시 잡으려는 교착 가능성을 제거한다. 이번 장애의 Python 사용자 스택은 확보하지 못했으므로 관측된 정지의 원인을 이것으로 확정하지는 않는다.

resource-guard는 루프가 완료될 때만 tmpfs `/tmp/resource-guard-heartbeat.json`을 갱신한다. 호스트 감시기는 30초마다 `io.coolify-fixkit.watchdog=true`와 Compose 서비스 `resource-guard` 라벨을 **모두** 가진 감시기만 확인한다. 시작 후 120초 유예, 생존 신호 120초 초과 시 감시기만 재시작하며 컨테이너별 10분 cooldown을 둔다. 애플리케이션, Incus, Caddy는 이 watchdog의 대상이 아니다. 라벨은 정규 Compose에 포함해야 재배포 후에도 유지된다.

호스트 감시기와 Discord 감시 스레드는 완료된 감시 루프에서만 systemd watchdog에 알린다. `WatchdogSec=90`을 넘겨 진행이 멈추면 systemd가 해당 보조 서비스만 복구한다. host unit의 RuntimeDirectory와 ReadWritePaths 변경도 함께 설치해야 한다. 네이티브 설치 디렉터리에는 `monitor_runtime.py`, `resource_guard.py`, `event_webhook.py`가 필요하다.

전체 흐름은 다음과 같다.

1. **호스트 보호:** 5초마다 guest SSD 대역폭과 각 guest의 memory.high를 조정하고 중요 프로세스의 OOM 우선순위를 유지한다. hard RAM과 IOPS는 보존한다. 프로세스별 I/O는 60초마다 수집한다.
2. **자원 감시:** RAM 여유, PSI, guest RAM/swap/한도, 계층 OOM 카운터와 증가 추세를 확인한다. host와 guest의 계층 카운터를 별도 OOM으로 더하지 않는다.
3. **OOM 복구:** 허용 라벨이 있고 최근 OOM으로 이미 종료된 컨테이너만 검사한다. 자원이 안정되고 새 배포가 없으며 cooldown과 시작 예산을 통과한 경우에만 start한다. 실행 중인 업무를 종료하지 않는다.
4. **개인 알림:** 3회 연속 압박/해소 및 OOM 변화를 Unix socket과 기존 Discord bot DM으로 알린다. 영속 outbox, 중복 제한, retry와 nonce를 사용한다. 공유 표본 지연과 통신 시간 때문에 정확히 15초마다 보장되는 알림은 아니다.
5. **TLS:** 반복 TLS 실패를 관찰한다. 기존 중요 프록시의 최후 수단 재시작은 별도의 명시적 설정 두 개가 모두 켜져야 한다.
6. **자체 감시:** 실제 진행 시각과 systemd watchdog으로 Healer 자체가 멈추는 것을 감지하고 해당 감시기만 복구한다.

2026-11-04 RAM 기준: 각 Incus의 memory.high를 실제 memory.max의 93%로 설정한다(커널 페이지 크기로 내림). guest 여유 판단은 hard 한도의 93%와 별도 soft 한도 중 더 낮은 값과 비교하며, soft 한도에 여유율을 다시 곱하지 않는다. 시작 메모리 예산도 같은 경계에 포함한다. 호스트 4GiB reserve, PSI 압박 감지와 중요 서비스 OOM 보호는 계속 적용한다. 이는 Incus 단위 정책이며 내부 Docker 컨테이너마다 새 RAM 상한을 배분하는 설정은 아니다.

## 실제 확인할 사건만 알림 (2026-11-04)

기존의 짧은 압박/해소 DM 쌍은 폐기한다. 실시간 알림은 커널 OOM 종료, 장시간 자원 압박 중 반복 관측된 상위 작업, 실제 복구 요청과 보호 기능 오류에 한정한다. 보호 기능의 같은 오류는 내용별 30분 동안 중복을 제한한다.

- OOM은 계층 카운터 증가를 감지하면 즉시 알린다. 보존된 커널 종료 기록에서 프로세스와 PID/Incus를 확인하고, 종료 주체가 커널이라는 사실을 명시한다. I/O 대기율만 보고 서비스 종료나 SSD 고장을 단정하지 않는다. Healer가 업무를 강제 종료하는 기능은 없다.
- RAM 93% 접근 또는 메모리 PSI, I/O PSI 압박이 10분 이상 이어지고, 동일 PID/starttime의 작업이 상위 자원 사용 표본에 3회 이상 관측돼야 장기 점유 DM을 보낸다. 측정된 RSS 또는 read/write 속도, 대상과 지속 시간, 확인할 작업을 적는다. 동시 관측을 인과관계로 단정하지 않는다. 같은 사건의 후속 알림은 6시간에 한 번으로 제한한다.
- 짧은 압박과 해소는 DM을 보내지 않는다. 정상 상태가 60초 유지되면 아직 발송되지 않은 압박 알림도 취소한다. 15분 넘게 지연된 압박 알림은 폐기한다. 새 OOM 사건은 이전 OOM의 cooldown 때문에 누락시키지 않는다.
- 상위 작업은 기존 60초 프로세스 I/O 수집에서 함께 계산해 tmpfs에 공유한다. 프로세스 인자와 환경변수는 수집하지 않는다. 지속 시간은 새 표본의 시각으로 계산하며 감시 공백은 연속 압박으로 세지 않는다.
- `coolify-fixkit-weekly.timer`는 매주 월요일 09:00 Asia/Seoul에 Now Brief를 요청한다. 서비스와 타이머를 `/etc/systemd/system/`에 설치하고 `systemctl enable --now coolify-fixkit-weekly.timer`로 활성화한다. 주간 브리프는 기존 journal을 스트리밍 집계하므로 별도 대형 시계열 파일이나 DB를 만들지 않는다.
- 주간 자료에는 최근 7일의 실제 관측 기간/표본 수, 최근 커널 OOM 종료 기록(최대 4개), guest별 RAM 피크와 93% 접근 표본 수, 블록 쓰기 증가량, I/O 상위 작업과 우선 확인 항목을 넣는다. cgroup 교체·카운터 초기화 구간은 합산하지 않는다. 표본 기반 결과이며 실제 서비스 응답 상태나 NAND 쓰기량은 아니다. 같은 ISO 주의 브리프는 영속 발송 기록으로 중복을 막는다.

`python3 weekly_brief.py --print`는 전송 없이 브리프를 미리 보는 명령이다. Discord bot 토큰은 기존 네이티브 알림 서비스만 읽는다.
