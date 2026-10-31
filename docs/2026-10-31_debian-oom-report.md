# Debian 및 Incus OOM 조사와 개선안

조사일: 2026-10-31 KST. 대상은 실제 Debian 호스트와 그 아래 모든 Incus 프로젝트/인스턴스다. `fedora-enxsis`, `fedora-heart`, `fedora-musecat`은 실행 중이고 `fedora-overclock`은 중지 상태다. 후자는 설정만 확인했고 시작하지 않았다.

**주요 원인은 메모리 용량에 비해 큰 빌드 동시성, `fedora-heart`의 스왑 차단, 작업량보다 낮은 soft limit, 운영과 개발이 공유하는 단일 SATA SSD다.** SSD 오류가 현재 장애의 원인이라는 증거는 없다. 초기 조사에서는 Healer 개선본 작성과 격리 검증까지 수행했다. 이후 비중단 운영 조치를 적용했으며 최신 상태는 문서 끝의 후속 조치 절을 따른다.

## 판단의 근거와 한계

| Evidence | Finding | Path |
|---|---|---|
| 10/30 OOM의 anon 약 15.54GiB, swap 한도 0, Java 약 8.69GiB, clang++ 약 3.92GiB | 동시 빌드가 guest 한도에 도달했고 데스크톱 프로세스가 피해를 입음 | 빌드 슬롯/메모리 예산을 먼저 제한하고 제한된 swap 재검토 |
| 같은 OOM의 `workingset_refault_file=86841597`, file 약 0.99GiB 중 shmem 약 0.99GiB | 일반 파일 캐시가 거의 남지 않은 상태에서 반복 재읽기 | anon 작업량 감소, soft limit/스왑 조정 후 재측정 |
| SSD 읽기 약 460MiB/s, util 약 100%, host I/O PSI full 최대 약 68% | 공유 SSD 채널 포화가 운영 지연으로 전파됨 | 개발 bulk I/O 제한 및 별도 SSD 분리 |
| SSD 오류/CRC/재할당 0, Btrfs device error 0, 낮은 부하 직접 읽기 중앙값 0.136ms | 현재 물리 고장이나 상시 저속 장치로 단정하기 어려움 | 교체는 성능 분리와 노후 대비 목적, 고장 단정은 보류 |
| 기존 Healer가 TLS 검사 실패 후 인증서 이동, 프록시 재시작에 약 165초 소요 | 자원 정체를 인증서 문제로 처리할 수 있고 감시도 멎을 수 있음 | 인증서 조작 제거, 자원 확인, timeout 및 영속 cooldown |
| 10/31 14시 DB 백업이 CPU 17.15초를 쓰고 wall time 10분으로 timeout | 개발 측 압박과 같은 시간대에 운영 유지보수도 영향을 받음 | 백업 검증 회복을 운영 지표에 포함 |

원본 수집 자료는 개발 환경의 `/home/heart/.local/state/oom-investigation-20261031/`에 보존했다. 일별 비식별 요약은 [daily-summary.csv](evidence/daily-summary.csv)다. 이 보고서에는 인증 정보, 전체 환경변수, 디스크 일련번호를 넣지 않았다.

sysstat는 10/24~10/31의 1,093개 약 10분 표본을 분석했다. 마지막 날은 14:10까지의 부분 집계다. PCP는 10/30 13:00~16:20을 1분 간격으로 추가 분석했다. 커널 저널은 30일을 요청했지만 현재 보존분은 주로 10/26 이후여서, 과거 30일 OOM 전수 기록으로 해석할 수 없다. 기존 PCP archive에는 cgroup별 메모리 지표가 기록되지 않아 각 guest의 장기 피크는 복원할 수 없었다. guest별 표는 조사 당시의 짧은 관측이다.

조사 중 `fedora-heart`가 14:28경 다시 시작되어 작업 세션도 중단됐다. Debian 호스트 uptime은 약 41일로 유지됐다. 해당 구간에 새 kernel OOM 로그는 확인되지 않았으므로 그 재시작을 OOM이라고 단정하지 않는다. 이 작업은 운영 Incus를 재시작하지 않았다. 재생성된 cgroup의 0 카운터는 과거 OOM이 없었다는 뜻이 아니다.

## 하드웨어와 저장장치

| 항목 | 실제 확인값 | 의미 |
|---|---|---|
| CPU | Xeon E5-2697 v3, 14코어/28스레드 | CPU 수를 기준으로 병렬 빌드를 늘리면 RAM이 먼저 부족해질 수 있음 |
| 메인보드 | MACHINIST X99 PR9, BIOS 5.11, 2023-10-10 | 확장과 NVMe 부팅 가능 여부는 실제 슬롯/BIOS 확인 필요 |
| RAM | 설치 32GiB, OS 가용 총량 약 31.2GiB | 동시에 여러 JVM/C++ 빌드와 데스크톱/운영 DB를 돌리기에는 여유가 작음 |
| RAM 구성 | DMI는 32GiB DIMM 하나, DDR4 3200 표기/실동작 2133MT/s | 실제 슬롯과 채널 구성은 실물 확인 필요. DMI의 최대 용량/슬롯 수를 구매 근거로 단정하지 않음 |
| GPU | GeForce GTX 660 Ti, nouveau | OOM 로그에 firmware 관련 오류도 있으나 이번 메모리/SSD 정체의 원인으로 입증되지 않음 |
| 운영 SSD | Samsung `MZNLN512HMJP-000`, 512GB, M.2 **SATA** 6Gb/s | M.2 모양이지만 NVMe가 아님 |
| SSD 배치 | `/dev/sda2`: Debian/Btrfs/Incus/Docker, `/dev/sda3`: 약 14.9GiB swap | 개발 캐시/빌드, 운영 DB, swap이 같은 물리 장치와 대역폭을 공유 |
| 추가 HDD | Hitachi 750GB + Toshiba 750GB의 RAID0, 별도 WDC 750GB XFS | RAID0에는 디스크 고장 내성이 없고 세 장치 모두 노후 지표 존재 |

조사 시 CPU package 약 60°C, SSD 37°C였다. 현재 샘플만으로 열 스로틀링이 원인이라는 증거는 없다. CPU microcode 관련 경고와 kernel의 CPU_OUT_OF_SPEC 표시도 있으나 원인으로 확정하지 않았고 펌웨어 업데이트/재부팅을 하지 않았다. DMI의 ECC 표기가 일관되지 않아 ECC 작동 여부도 확정하지 않았다.

### SSD 건강과 실제 성능

SMART 통과, 사용 시간 31,262시간, 재할당/프로그램 실패/erase 실패/uncorrectable/CRC 각각 0이다. Wear Leveling의 정규화 값은 52, Media Wearout 정규화 값은 50이다. 제조사별 속성이므로 이를 정확한 잔여 수명 50%라고 단정하지 않는다. attribute 241의 raw 값을 임의로 512바이트 LBA로 환산하지 않았다. 예기치 않은 전원 손실 누적은 1,793회다.

Btrfs device error 5종은 모두 0이고, 10/22 scrub은 오류 없이 완료됐다. 파일시스템 추정 여유는 약 200.6GiB로 공간 고갈 징후는 없다. 이 결과가 향후 고장을 배제하는 것은 아니다.

| 측정 | 결과 | 해석 |
|---|---|---|
| 운영 중 10분 평균 최고 읽기 | 약 460.7MiB/s | 실제 부하가 SATA SSD 대역폭을 채우는 구간 존재 |
| 포화 구간 평균 I/O 대기 | 약 5~10ms, queue 약 27~31 | 여러 작업이 같은 장치에서 기다림 |
| 4KiB 직접 읽기 QD1, 400회 | p50 0.136ms / p95 0.306ms / p99 2.639ms / max 5.621ms | 당시 낮은 부하에서의 응답 지연. 최대 IOPS/성능 보증치는 아님 |

읽기 검사는 `O_RDONLY|O_DIRECT`로 총 1.56MiB만 읽었고 초당 100회를 넘지 않게 했다. 쓰기/지속 쓰기/캐시 소진 벤치마크는 운영 장치에 실행하지 않았다. 순차 읽기 최고치는 인위적인 벤치마크가 아닌 기존 운영 관측치다.

HDD는 모두 SMART 통과, 재할당/pending/uncorrectable/CRC 0이다. 다만 WDC는 39,215시간과 load cycle 약 183만 회, RAID0의 두 디스크는 각각 load cycle 약 85만/61만 회다. swap이나 고빈도 DB를 이 HDD로 옮기는 것은 이번 지연의 해결책으로 선택하지 않는다.

## 사용 동향

| 날짜(KST) | 최소 available GiB | 최대 swap GiB | 최대 I/O wait | 최대 load1 | 최대 SSD util |
|---|---:|---:|---:|---:|---:|
| 10/24 | 8.61 | 1.78 | 21.01% | 87.21 | 47.50% |
| 10/25 | 2.60 | 2.44 | 29.60% | 233.19 | 51.44% |
| 10/26 | 5.09 | 4.56 | 18.15% | 46.08 | 48.16% |
| 10/27 | 2.70 | 9.85 | 49.21% | 308.62 | 99.99% |
| 10/28 | 8.60 | 4.42 | 2.53% | 13.83 | 23.79% |
| 10/29 | 3.71 | 10.04 | 18.01% | 265.88 | 57.96% |
| 10/30 | 3.30 | 7.91 | 60.22% | 366.88 | 99.50% |
| 10/31 일부 | 9.94 | 7.18 | 61.59% | 187.27 | 100.00% |

각 열의 최대/최소가 같은 시점이라는 뜻은 아니다. CPU I/O wait와 disk rate는 직전 표본 간 평균이며 available/load는 표본 시점 값이다. 10분 자료는 수초의 OOM 직전 피크를 놓칠 수 있다.

10/28은 비교적 안정적이지만 10/27, 10/30, 10/31에는 반복적인 읽기 포화가 있다. 10/30 PCP 분석에서 메모리 PSI some avg10은 최대 85.97%, I/O PSI full avg10은 최대 67.98%였다. 13:33에는 available 약 9.09GiB인데 메모리 PSI some이 약 85%였다. 호스트 available만 보는 경보로는 이런 cgroup 압박을 놓친다. 이 패턴은 단순 CPU 부족보다 reclaim/대기 정체를 설명한다.

### Incus별 상태

| 인스턴스 | hard max | memory.high | swap max | 당시 current / swap | 판단 |
|---|---:|---:|---:|---:|---|
| fedora-enxsis | `6GB`, 실제 약 5.59GiB | 3GiB | 1GiB | 약 1.18GiB / 0.37GiB | 관측 시 여유, 과거 cgroup 피크는 자료 없음 |
| fedora-heart | 18GiB | 12GiB | **0** | 약 11.59GiB / 0 | 데스크톱, 브라우저, AI 도구, 빌드가 같은 한도 경쟁 |
| fedora-musecat | 16GiB | 10GiB | 5GiB | 약 4.36GiB / 1.57GiB | 서비스는 비교적 작지만 백업/빌드 피크까지 예산 필요 |
| fedora-overclock | 8GiB | 미확인 | 미확인 | 중지 | 자동 시작하지 않음 |

실행 중 hard limit 합은 약 39.59GiB로 물리 RAM을 넘고 Debian/Coolify 예산도 별도로 빠진다. hard limit은 예약 메모리가 아니므로 합이 넘는 것 자체가 즉시 장애는 아니지만, 동시 피크를 허용할 수는 없다. `high` 합 약 25GiB도 커널/호스트와 함께 예산화해야 한다. `fedora-heart`의 CPU 한도 24, `fedora-musecat` 20 역시 동시에 보장되는 CPU 수가 아니다.

현재 root disk의 각 7,200 read IOPS / 12,600 write IOPS 설정은 집계된 전체 SSD 예산이 아니다. 큰 블록의 스트리밍 읽기는 낮은 IOPS만으로 대역폭을 채울 수 있다. 따라서 IOPS 제한이 있어도 약 460MiB/s의 포화를 막지 못한다.

### 실제 OOM 분류

보존된 운영 로그에서 확인된 8건은 모두 `CONSTRAINT_MEMCG`였다. 테스트로 만든 OOM은 아래 운영 집계에서 제외한다.

- 10/26 13:28:38: `fedora-heart` 전체 cgroup에서 Chromium 계열 2개 종료.
- 10/29 18:36~18:44: `cosai-fold7-avd.service` 내부에서 QEMU 5회 종료. 각 종료의 anon RSS가 약 1.96GiB여서 서비스 자체 한도 문제도 따로 다뤄야 한다. 당시 설정 파일을 복원하지 못했으므로 정확한 과거 MemoryMax 값은 단정하지 않는다.
- 10/30 14:03:22: `fedora-heart` 전체 cgroup 한도에서 데스크톱 앱 종료. 당시 한도는 18,000,000,000바이트에 해당하는 약 16.76GiB였고 현재 18GiB 설정과 다르다. 익명 메모리는 약 15.54GiB, swap limit 0이었다.

마지막 OOM의 task table을 프로세스명별 anon RSS로 합산하면 Java 5개 약 8.69GiB, clang++ 26개 약 3.92GiB, 데스크톱 앱 계열 18개 약 0.89GiB다. 죽은 프로세스와 메모리 증가를 일으킨 프로세스는 같지 않다. 이 기록만으로 특정 앱의 누수를 단정하면 빌드 동시성 문제를 놓치게 된다.

`memory.high`를 넘으면 해당 cgroup이 강한 회수/스로틀링을 받고, hard max에 도달해 회수할 수 없으면 cgroup OOM이 발생한다. 따라서 high를 지나치게 낮추는 설정은 OOM 전 장시간 정체를 만들 수 있다. 이 동작은 [Linux cgroup v2 문서](https://docs.kernel.org/admin-guide/cgroup-v2.html)에 명시돼 있다. 이번 관측은 `스왑 없는 큰 anon → 파일 캐시 회수 → 재읽기 → 공유 SSD 포화` 경로와 일치한다. 모든 정체 구간의 개별 프로세스 I/O 자료가 있는 것은 아니므로 모든 구간의 원인을 동일하게 확정하지는 않는다.

호스트 zswap은 이미 lz4, 최대 RAM 20%로 활성화돼 있다. 당시 압축 풀은 약 902MiB, stored pages는 약 2.38GiB에 해당했다. 따라서 zswap 추가 설치를 해결책으로 제안하지 않는다. `memory.swap.max=0`인 guest는 별도로 검토해야 한다. zswap은 CPU를 써 swap I/O를 줄이는 압축 캐시이고 용량 부족을 제거하는 장치는 아니다. [Linux zswap 문서](https://docs.kernel.org/admin-guide/mm/zswap.html).

## 기존 Healer 문제와 구현한 변경

실배포는 `bampudding/CoolifyFixKit`, main, commit `49feb0e756553a585f62ba6548535f36d01e22c5`였다. Debian Coolify의 기존 애플리케이션 ID 28이며 조사 당시 약 5주 연속 실행 중이었다.

기존 코드는 curl의 모든 실패를 SSL 오류로 누적한 뒤 인증서 디렉터리를 이동하고 프록시를 재시작할 수 있다. DNS 조회 성공이나 Caddy route 존재만으로 timeout 원인이 인증서라고 판단할 수는 없다. 10/31 10:25:33 KST에 실제 인증서 이동과 프록시 재시작을 수행했고 restart 명령은 약 165초 뒤 반환됐다. 10:30 sysstat에는 I/O wait 37.15%, 읽기 약 328.6MiB/s가 기록돼 있다. 당시 인증서 자체가 고장났다는 독립 증거는 수집되지 않았다.

리로드 후보는 실제 생성 설정과 일치한다는 보장이 없었고, 성공 후 도메인 cooldown을 0으로 초기화해 장애가 반복되면 다시 동작할 수 있었다. 순차 도메인 검사 때문에 실제 주기는 5초보다 길어질 수 있었다.

이번 변경은 다음과 같다.

1. `resource_guard.py`: 기본 5초 간격으로 호스트와 모든 Incus root cgroup의 메모리/PSI/OOM/refault 지표 기록. boot/inode가 바뀌면 카운터 차분을 초기화한다. 짧은 증가율 예상은 경고만 만들고 종료 판단에 사용하지 않는다.
2. `check` 모드: available 4GiB 예산과 추가 작업량, guest 압박을 확인해 빌드 진입점이 실행을 보류할 수 있는 종료 코드 제공. 현재 운영 빌드에는 아직 연결하지 않았다.
3. 복구: 라벨로 지정하고 최근 24시간 내 OOM으로 종료된 컨테이너만 시작. 기존 ID, 최신 배포/상태 재조회, guest headroom, 영속 cooldown 및 시간/일 예산 확인. 모든 subprocess에 timeout 적용.
4. Incus 복구는 별도 소켓 opt-in. 실제 guest init PID에서 경로를 찾으며 Docker의 private cgroup namespace가 표시하는 `/../../` 경로도 안전하게 처리한다.
5. TLS 감시: 인증서 이동/삭제와 추측한 설정 리로드 제거. 자원 정체/측정 실패 시 재시작 보류. 프록시 전체의 영속 cooldown 적용. 기본 관찰 모드.
6. Compose: 읽기 전용 proc/cgroup 마운트, 128MiB/0.25CPU/64 PID 제한, 로그 순환과 상태 볼륨. 인증서 경로 접근을 위한 `/data/coolify` 마운트 제거.

로그만으로 강제 예방이 완성되는 것은 아니다. `check`를 실제 빌드 진입점에 연결하고 동시 진입 잠금/슬롯을 추가해야 한다. 임의 빌드 프로세스 종료, memory.max 자동 확장, cache 강제 비우기, swapoff, Docker daemon/호스트/guest 전체 재시작은 구현하지 않았다. 이 처리는 개발 상태나 데이터에 영향을 줄 수 있고 용량 문제를 숨길 수 있다. 실행 중인 컨테이너 내부 일부 프로세스만 OOM으로 죽은 경우에도 무조건 컨테이너를 재시작하지 않는다.

## 권장 적용 순서

### 1. 빌드 작업량부터 제한

현재 32GiB 장비에서는 무거운 빌드 동시 실행을 우선 1개로 제한하고 CPU 병렬도만으로 작업을 늘리지 않는다. 시작안은 C/C++ compile jobs 4, Gradle workers 2/parallel false, Gradle daemon heap 2GiB다. Gradle 외의 Kotlin/native worker 메모리도 포함해 빌드 전체 peak를 따로 측정해야 한다. [Gradle 환경 설정](https://docs.gradle.org/current/userguide/build_environment.html), [Gradle JVM 설정](https://docs.gradle.org/current/userguide/config_gradle.html).

장시간 유지되는 daemon을 작업별로 중복 생성하지 않고 idle 후 정리하도록 한다. 같은 작업을 여러 세션이 시작하지 않도록 호스트 공통 슬롯을 쓴다. 사전 `check`는 관측일 뿐 예약이 아니어서 단독으로 동시 진입을 막지는 못한다.

현재 메모리에서 목표는 Debian/Coolify와 커널에 최소 4GiB, 추가 여유 2GiB를 남기고 실행 작업의 합계 working set을 대략 25GiB 이하로 유지하는 것이다. guest별 장기 피크 자료 없이 hard cap을 즉시 낮추지는 않는다.

### 2. fedora-heart 스왑/soft limit을 단계적으로 조정

빌드 제한 후 `fedora-heart`의 bounded swap 2GiB부터 관찰하고, 필요하면 4GiB까지 평가한다. high는 실제 작업의 anon+커널+필수 file working set보다 낮지 않게 조정한다. 예를 들어 현재 high 12GiB 대신 14~15GiB를 평가할 수 있지만, 이는 호스트 동시 예산을 확보한 뒤의 후보값이며 고정 적용안이 아니다. hard max를 계속 늘리는 방식은 물리 RAM 부족으로 문제를 옮길 수 있다.

Incus의 `limits.memory.swap`에는 바이트 한도를 지정할 수 있고 cgroup v2에서는 swap priority 설정 효과가 없다. 적용 뒤에는 실제 `memory.swap.max`를 검증한다. [Incus instance options](https://linuxcontainers.org/incus/docs/main/reference/instance_options/).

host zswap은 유지하고 swapoff나 cache drop으로 작업을 밀어내지 않는다. swappiness는 현재 40이며 과거 설정 파일에는 10도 남아 있다. 먼저 설정 소유자를 정리하고 PSI/refault를 기준으로 평가한다. 단일 swappiness 값만 바꿔 해결된다고 보지 않는다.

### 3. 운영과 개발의 I/O 경계 설정

메모리 압박 완화 후 개발 bulk 작업에 우선 byte/s 예산을 검토한다. 초기 후보는 개발 읽기 약 100~150MiB/s, 쓰기 약 30~50MiB/s이며 실제 업무의 지연을 측정해 조정한다. 이미 회수 중인 guest에 I/O cap만 먼저 낮추면 정체가 더 길어질 수 있으므로 순서를 지킨다. root 이외의 추가 volume과 같은 물리 장치의 관계도 확인한다. IOPS와 byte/s 설정 의미는 [Incus disk options](https://linuxcontainers.org/incus/docs/main/reference/devices_disk/)를 따른다.

현재 dirty 비율은 background 3%, foreground 10%다. 대용량 쓰기 burst가 남으면 고정 background 128MiB / dirty 512MiB 등의 후보를 별도 검증할 수 있다. 그러나 이번 주요 구간은 읽기/refault 포화이므로 writeback 설정만으로 해결하지 않는다. live SSD에서 Btrfs balance/defrag/대규모 scrub을 장애 대응 작업으로 실행하지 않는다.

### 4. 자동 복구를 제한된 대상부터 활성화

기존 Coolify 리소스에서 관찰 모드로 배포한 뒤, 유실 가능한 상태가 없는 서비스 하나의 라벨과 실제 시작 메모리 예산부터 등록한다. guest Docker 복구가 필요할 때만 Incus socket opt-in을 합친다. 상태/volume을 유지하고 중복 인스턴스를 생성하지 않는다.

systemd 관리 에뮬레이터와 데스크톱에는 개별 서비스 정책이 별도로 필요하다. 현재 데스크톱은 이미 Restart=on-failure/OOMPolicy=stop이다. 에뮬레이터는 조사 시 inactive라 재실행하지 않았다. 필요 메모리를 측정해 서비스 한도를 수정하고 StartLimitIntervalSec/StartLimitBurst로 재시도 폭주를 막는 방식을 검토한다. [systemd service](https://www.freedesktop.org/software/systemd/man/devel/systemd.service.html), [systemd unit](https://freedesktop.org/software/systemd/man/devel/systemd.unit.html).

### 5. 장비 개선

지금 작업 형태를 유지하려면 RAM 총 64GiB 이상, 다중 native 빌드를 계속 병행하려면 96~128GiB를 검토하는 것이 타당하다. 실제 보드 슬롯/지원 모듈/채널/ECC 작동 여부를 확인한 뒤 구성한다. 단일 DIMM의 병목 가능성도 이때 검증한다. 이 용량은 측정한 작업량에 근거한 설계 권고이며 실제 확장 호환성 확인이나 구매를 완료한 것은 아니다.

저장장치는 개발 build/cache/container layer를 별도 SSD로 분리하는 것을 우선한다. 사용 가능한 PCIe 경로가 확인되면 NVMe를 후보로 삼을 수 있지만 보드 호환성과 부팅 지원을 단정하지 않는다. 기존 SSD 교체만 하고 병렬 작업량/스왑 정책을 그대로 두면 cgroup OOM은 남을 수 있다.

HDD RAID0는 유일한 백업본의 목적지로 삼지 않는다. 14시 실패한 DB 백업을 다음 정상 주기에 재검증하고 독립 매체의 복원 가능성을 확인한 뒤 storage 변경을 진행한다. 같은 장애 시간대 API monitor의 HTTP 400도 발견했지만, 원인을 OOM으로 확정하거나 이 조사에서 앱 정책을 변경하지 않았다.

### 6. 2~4주 추세로 보정

sysstat 보존 31일과 적절한 수집 주기를 검토하고, PCP 또는 guard의 guest별 current/anon/refault/high/OOM 지표를 보존한다. 하루 중 반복되는 빌드/백업 시간을 비교해 슬롯을 조정한다. 완료 기준은 OOM 횟수뿐 아니라 PSI 지속 시간, file refault, SSD queue, 요청 지연, 백업 성공률이다. 예: 일반 운영 구간 memory PSI some avg10 5% 미만, I/O full avg10 5% 미만을 목표로 두고 실제 SLA에 맞춰 보정한다.

## 검증 결과와 남은 적용

- Python 단위/회귀 검사 34개 통과. 쿨다운 영속성, 재생성 카운터, 새 배포 보호, 재시작 직전 압박, 손상 상태, TLS 오진/재시작 제한 포함.
- Bash 구문 검사와 Compose 기본 구성/Incus override 병합 검증 통과.
- Debian Docker에서 candidate 이미지 빌드 통과. 읽기 전용 root/cap_drop ALL/128MiB 제한의 임시 컨테이너에서 실행 중인 Incus 3개를 정상 관측했다.
- 동일 제한의 임시 컨테이너에서 Debian Docker와 guest Docker 3개의 목록 조회 성공. 실제 Incus 경로/프로젝트 처리까지 확인했다.
- 별도 64MiB 컨테이너 하나에서 의도적으로 cgroup OOM 발생 → 기존 ID 자동 시작 1회 → 실행 상태 확인 → 중복 시작 방지 확인. 테스트 컨테이너는 제거했다. 이 테스트 OOM은 운영 장애 집계에서 제외해야 한다.
- 운영 TLS/DB/Incus의 재시작, 자원 한도 변경, 인증서 조작, Git push 또는 Coolify 운영 재배포는 수행하지 않았다. 실제 guest Docker에서 OOM을 유발하는 시험도 하지 않았다.

후보 이미지는 Debian의 `coolify-fixkit:audit-20261031`, 검증 소스는 `/tmp/coolify-fixkit-audit-20261031`, 조사 증거는 앞서 명시한 개발 환경의 private state 경로에 있다. 수정된 정규 소스는 `/home/heart/GitHubH/CoolifyFixKit`이다. 해당 저장소에 기존 AGENTS.md는 없었고 Musecat 앱 소스와 사용자의 미커밋 작업은 변경하지 않았다.

운영 재배포, 빌드 진입점 연결, 동시 빌드 잠금, 대상별 복구 라벨 및 자원 정책 조정은 남아 있다. 따라서 이 결과를 이미 OOM이 예방되는 운영 상태라고 표현하지 않는다. 유한한 RAM에서 임의 작업량을 계속 허용하면서 OOM을 완전히 차단할 수는 없으며, 작업 진입 제어와 용량 계획을 함께 적용해야 한다.

## 후속 장애 제보 후 재검증과 실제 적용

사용자가 추가 장애와 SSD 과소비, Incus RAM 설정 불일치를 제보해 14:44 이후 다시 조사했다. **이 절의 운영 적용 상태가 앞선 초기 조사 시점의 미적용 상태보다 최신이다.** 기존 Coolify 애플리케이션의 Git push/재배포는 하지 않았으며, 호스트 보조 구성요소를 별도로 설치했다.

### 설정값, 커널 한도, 앱 인식값의 차이

14:44 기준 `fedora-heart`의 설정은 이전의 18GiB/24 CPU에서 `16GB`/18 CPU로 변경돼 있었다. 이 후속 작업에서 그 hard RAM/CPU 값을 바꾸지 않았다. 실제 `memory.max=16000000000`, LXCFS MemTotal 16,000,000,000바이트로 일치했다. `16GB`는 약 14.90GiB이므로 `free -h`의 14Gi 표기는 일부 RAM이 사라진 결과가 아니다.

반면 저장된 raw `memory.high=12884901888`과 실제 kernel `memory.high=max`가 달랐다. `/run/incus/.../lxc.conf`에도 시작 당시의 18GiB 값이 남아 있었지만 live kernel은 16GB였다. 생성된 시작 파일만 읽으면 현재 적용값을 잘못 판단한다. Incus live update 뒤 raw soft 한도가 따라가지 않는 운영 불일치를 확인했다.

Node 26.3.0과 Bun 1.4.4의 `os.totalmem()`은 호스트 전체 33,482,227,712바이트를 반환했지만 `process.constrainedMemory()`는 실제 16GB를 반환했다. Java 25.0.5의 플랫폼 Metrics 출력은 Unlimited였지만 HotSpot trace는 부모 cgroup까지 올라가 실제 16GB를 발견했고 기본 heap도 약 4GB였다. 따라서 Java heap까지 호스트 전체 기준이라고 단정하지 않았다. 커널 hard limit을 우회하는 현상도 확인하지 않았다.

LXCFS의 `/proc/meminfo` 가상화와 `sysinfo()` syscall의 의미가 다르다. [Incus 설정 문서](https://linuxcontainers.org/incus/docs/main/reference/instance_options/)의 sysinfo interception은 재시작이 필요한 옵션이고, [Incus 구현](https://github.com/lxc/incus/blob/main/internal/server/seccomp/seccomp.go)은 privileged 인스턴스에서 interception을 건너뛴다. 현재 heart는 privileged이므로 플래그만 켜면 모든 앱의 RAM 인식이 바뀐다고 제안하지 않는다. privilege 변환, syscall shim 주입, Docker daemon/운영 guest 재시작은 하지 않았다. 메모리 기반 작업량 계산에는 실제 cgroup 한도를 써야 한다.

### SSD 소모량과 원인 추적 범위

`sadf`의 `wkB/s × 표본 간격`을 적분하면 10/24~10/30 SSD 쓰기는 하루 약 289.6~470.1GiB였다. 10/30 읽기는 약 2,270GiB, 10/31 14:10까지 약 1,862GiB였다. 읽기 폭주와 쓰기 내구성 소모를 구분해 다뤘다. discard는 별도 지표이며 NAND 쓰기로 합산하지 않았다.

코어 덤프는 비어 있었고 guest Docker JSON 로그 합계는 당시 enxsis 약 6MB, heart 약 40MB, musecat 약 13MB였다. 이것만으로 하루 수백 GiB를 설명할 수 없다. hourly verified DB backup은 회당 약 1GB 규모여서 기여는 있으나 전체 쓰기를 단독으로 설명하지 못한다. 백업 주기나 보존본을 임의로 줄이지 않았다.

짧은 프로세스 관측에서는 PostgreSQL checkpoint, PocketBase와 Btrfs transaction/writeback, 개발 도구의 쓰기가 보였다. 짧은 표본을 하루 전체 비율로 외삽하지 않았다. 기존 archive에는 원인을 완전히 귀속할 수 있는 프로세스 I/O 이력이 없으므로 새로 60초 간격 귀속 기록을 시작했다. `host-io`에 guest별 누적 rbytes/wbytes와 PID/comm/cgroup별 read/write rate가 남는다. 반복되는 실제 개발 빌드와 운영 부하에서 소비 주체를 구분할 수 있다.

### 비중단으로 적용한 조치

1. Debian에 `coolify-fixkit-host.service`를 설치하고 활성화했다. 일반 동작에서는 약 15MB RAM을 사용하며 실제 제한은 96MB/0.1 CPU다. source는 저장소의 `host_guard.py`, 실행 파일은 `/usr/local/lib/coolify-fixkit/host_guard.py`, 정책은 `/etc/coolify-fixkit/host-policy.json`이다.
2. 기존 IOPS 7,200/12,600을 유지하고 heart 128/48, enxsis 64/16, musecat 128/64MiB/s의 read/write 상한을 추가했다. 설치된 Incus 7.0.1은 `128MiB,7200iops` 문자열을 거부했으므로 kernel io.max의 B/s 항목만 별도로 유지한다. 합계 읽기 상한은 320MiB/s로 운영 SSD의 기존 포화 관측치보다 낮다. 호스트의 다른 작업은 이 합계에 포함되지 않는다.
3. heart hard RAM 16GB는 그대로 두고 soft 한도를 실제 hard의 90%, 즉 14,400,000,000바이트로 적용했다. 오래된 raw high 한 줄을 제거해 보조 서비스가 단일 소유자가 되게 했다. live update로 high/B/s가 초기화되면 보조 서비스가 복원한다.
4. heart swap을 0에서 bounded 2GiB로 live 변경했다. 호스트의 기존 lz4 zswap은 유지한다. 다른 인스턴스의 RAM/swap hard cap은 변경하지 않았다.
5. heart/root 사용자 홈에 새 Gradle 기본값을 만들었다. workers 2, parallel false, build cache true, Gradle heap 2GiB, Kotlin daemon heap 1GiB다. 기존 해당 파일이 없음을 확인한 뒤 생성했다. 전역 JVM heap을 강제로 덮어쓰지 않았다.
6. 새 shell의 CMake/Cargo 병렬도 기본값을 4, CTest를 2로 두었다. 명시적인 호출자 옵션과 기존 실행 중인 프로세스는 바꾸지 않는다. bash/zsh 구문 및 새 zsh의 환경값을 검증했다. 중앙 빌드 슬롯을 모든 도구에 강제로 연결한 상태는 아니다.
7. 후보 Resource Guard도 정상 로그를 60초로 줄이고 OOM/위험 변화는 즉시 기록하도록 수정했다. 후보 Docker 로그 상한은 128MiB다. 이 후보 애플리케이션은 아직 운영 재배포하지 않았다.

운영 인스턴스, DB, Docker daemon을 재시작하지 않았고 기존 데이터/백업을 삭제하지 않았다. 정책 전 원본은 Debian `/root/fixkit-performance-20261031/*.before.yaml`에 보관했다. 초기 조사에서의 테스트 OOM 2건은 14:33과 14:41의 별도 64MiB Docker cgroup이며, 이 후속 작업에서 새로운 OOM 시험은 하지 않았다. 14:28 재시작을 kernel OOM으로 확정할 추가 기록은 확인되지 않았다.

### 실제 적용 검증과 남은 범위

- 전체 회귀 검사 41개 통과. B/s만 갱신해 IOPS를 보존하는 경우, decimal RAM에 대한 high 계산, 잘못된 정책 거부, 중지 인스턴스 건너뛰기를 추가 검증했다.
- live high/B/s를 일시적으로 max로 초기화한 뒤 7초 이내 자동 복원을 확인했다. hard RAM과 기존 IOPS는 그대로였다.
- 서비스는 실행 중이고 재시작 횟수 0이다. CPU/RAM overhead와 guest별 io.max/memory.max/high/swap.max를 실제 파일로 확인했다.
- 적용 뒤 15시 DB verified backup은 약 31초에 성공했고 DB integrity 및 도메인 참조 검증도 통과했다. 이것만으로 앞선 장애가 영구 해결됐다고 판단하지는 않는다.
- 현재 유휴 부하에서는 I/O wait가 낮다. 이전 장애와 같은 작업량을 재현한 비교 시험은 하지 않았으므로 일일 쓰기 감소율이나 다음 빌드의 무장애를 아직 수치로 보장하지 않는다.

대역폭 상한은 폭주를 제한하지만 동일한 총 작업을 더 오래 실행할 수 있으므로 누적 쓰기 감소의 증거는 아니다. 실제 빌드 동시성/캐시와 프로세스별 쓰기 이력을 같이 평가해야 한다. hard RAM 합계도 여전히 호스트보다 커서 모든 인스턴스의 동시 최대 사용을 보장할 수 없다. 이를 숨기기 위해 hard cap을 임의로 키우거나 DB durability/fsync 설정을 약화하지 않았다.

## 중요 영역을 최후 순위로 두라는 사용자 결정

커밋/푸시 준비 중 사용자가 `fedora-musecat`, Debian Caddy, Incus는 OOM killer에서도 가장 나쁜 상황의 마지막 대상으로 둬야 한다고 명시했다. 이를 호스트 정책과 TLS 복구 기본값에 반영했다.

- host guard가 해당 cgroup의 프로세스를 -950 이하로 보호하고 heart의 명확한 compiler/Gradle/Kotlin worker는 +500 이상으로 설정한다. 기존의 더 강한 보호는 유지한다.
- musecat 전체에 memory.low 4GiB와 oomd avoid를 적용하고, 내부 system/user slice도 avoid로 설정했다. Incus는 memory.low 256MiB와 avoid, Debian Caddy는 avoid와 process priority 보호를 적용했다.
- 호스트 oomd는 활성화하지 않았다. 유한한 점수와 avoid를 사용해 최후 복구 선택지는 남겼다. OOM을 새로 유발하는 검증은 하지 않았다.
- 실제 확인 시 Incus/Caddy PID의 oom_score_adj는 -950, musecat의 당시 181개 프로세스 중 가장 높은 값도 -950이었다. hard RAM과 기존 IOPS는 보존했다.
- Caddy 재시작은 `SSL_CRITICAL_RESTART=true`라는 별도 명시적 최후 수단 허용 없이는 실행하지 않는다. 기본값은 false다.

중요 서비스의 자체 cgroup hard cap에 도달하는 경우에는 외부 개발 cgroup을 희생시킬 수 없으므로, 우선순위만으로 모든 OOM을 방지한다고 보장하지 않는다. 전체 회귀 검사는 47개가 통과했다. 이 조치에 musecat 서비스나 Caddy/Incus의 종료·재시작은 필요하지 않았다.
