# 최근 로그로 확인한 개선 우선순위

2026-10-31 15:26–16:25 KST의 host guard 약 1시간 기록과 최근 90분 커널 로그를 검토했다. 운영 서비스 재시작이나 부하 벤치마크는 실행하지 않았다. 이후의 사용량을 대표하는 장기 통계는 아니다.

## 확인한 상태

| 근거 | 관측 결과 | 해석 |
|---|---|---|
| 커널 로그와 현재 guest memory.events | 최근 90분 OOM/디스크 오류 없음. 현재 세 guest oom_kill=0 | 이 관측 구간의 압박 알림을 OOM 발생으로 해석하면 안 됨 |
| 호스트 meminfo/PSI | MemAvailable 약 17.6GiB, 메모리 PSI avg10=0 | MemFree 약 1GiB만 보고 RAM 고갈로 판단하면 오탐 |
| musecat 메모리 | 관측 최대 약 9.81GiB, soft 10GiB/hard 16GiB. 후속 스냅샷 anon 약 3.1GiB, file 약 5.5GiB | 현재 사용량의 상당 부분은 파일 캐시이며 단순 누수 증거가 아님 |
| musecat cgroup I/O 변화 | 약 6.48GiB 읽기, 8.09GiB 쓰기. 분 단위 쓰기 최고 약 40.6MiB/s | 누적 쓰기 원인과 순간 지연을 따로 관리해야 함 |
| I/O PSI와 배포 큐 | 15:43, 16:01에 host full avg10 각각 31.33%, 22.48%. Preview 배포 15:42–15:44, 16:00–16:02와 겹침 | 운영 guest의 배포·이미지 작업이 병목의 유력한 원인. 파일 단위 원인까지 확정한 것은 아님 |
| 평상시 I/O와 Healer 로그 | 후속 host I/O full avg10=0. Healer resource-guard 로그 약 0.17MiB, SSL 로그 약 0.00012MiB | 이 구간의 주된 SSD 쓰기를 Healer 로그만으로 설명할 수 없음 |
| SMART | 삼성 SATA SSD 512GB, 35°C, SMART 통과. 재할당/CRC/오류 로그 0, 가동 31,264시간, Wear_Leveling_Count 정규값 52 | 현재 고장 증거 없음. 정규값을 남은 수명의 확정 백분율로 환산하지 않음 |

cgroup I/O는 계층 누적값이다. 부모·자식 수치를 합산하면 중복 집계된다. 위 읽기/쓰기는 guest root의 두 관측값 차이다. 프로세스별 I/O는 단서이며 Btrfs writeback 및 짧게 실행된 작업까지 완전하게 귀속하지 못한다. 분 단위 최고 속도도 SSD의 최대 성능 벤치마크가 아니다.

## 즉시 보완한 Healer 결함

`host_guard.py`의 `protection-error: RuntimeError`가 두 번 있었다. 기존 코드는 Docker 조회와 전체 보호 적용을 같은 try 블록에서 처리해, 조회 실패 시 해당 주기의 Incus/guest 보호 재적용까지 건너뛰었다. 이미 적용한 보호가 해제된 것은 아니다. 기존 로그에는 stderr/세부 원인이 없으므로 두 실패의 근본 원인은 확정할 수 없다.

이번 변경은 Docker 조회 실패 시 마지막으로 확인한 Caddy cgroup을 유지하고, Incus 및 운영 guest 보호 적용은 계속한다. 최초 조회 실패로 캐시가 비어 있어도 이름이 고정된 guest/unit 보호는 적용한다. 조회는 5초 뒤 재시도하며 `container-discovery-error`에 캐시 수와 고정된 실패 분류/종료 코드를 남긴다. 원문 stderr, 환경변수, URL은 기록하지 않는다. `--once`는 조회 실패 시 보호 적용을 시도한 후에도 실패 종료 코드를 반환한다.

추가 조사에서 host guard cgroup의 `pids.events`에 `max 9`를 확인했다. `TasksMax=16` 한도에 실제 도달한 기록이다. 이 서비스는 Python뿐 아니라 Go 기반 Docker CLI의 스레드도 같은 예산으로 실행한다. 과거 stderr가 없어 두 오류와의 일대일 대응은 확정할 수 없지만 간헐적인 조회 실패의 유력한 원인이다. 보조 서비스에만 `TasksMax=64`, `GOMAXPROCS=2`를 적용하고, 앞으로 Go 스레드 생성 실패는 `thread-budget`으로 구분한다. 메모리 96MiB/CPU 10% 한도와 운영 서비스 설정은 유지한다.

같은 CPU/메모리 예산의 별도 transient unit에서 읽기 전용 Docker inspect를 반복해 비교했다. 기존 `TasksMax=16`에서는 12회 중 1회가 종료 코드 2로 실패했고 `pids.events max=25`가 기록됐다. 수정한 `TasksMax=64` 및 `GOMAXPROCS=2`에서는 24회 모두 성공했고 `pids.events max=0`이었다. 두 검사 모두 peak memory 약 21.7MiB였으며 운영 컨테이너의 시작·중지 작업은 없었다. 이 재현은 태스크 예산 결함을 확인하지만 과거 두 오류의 원문을 복원하는 것은 아니다.

Discord 압박 알림에는 여유 RAM, 메모리 PSI some avg10, I/O PSI full avg10을 넣었다. 압박/해소 전환 시 같은 시각의 구조화 로그도 기록해 다음 검토에서 근거 수치를 찾을 수 있다. 정상 5초 관측마다 로그/DM을 추가하지 않는다.

## 다음 조정의 순서

1. **운영 guest의 Preview 빌드·이미지 작업부터 분리한다.** 운영 서비스를 옮기거나 중지하기 전에 빌드 실행 위치, 배포 큐 동시성, 이미지 pull/extract, cache 재사용 여부를 확인한다. 빌드 전용 실행기를 쓰더라도 같은 물리 SSD를 공유하면 I/O 경쟁은 남는다. 필요한 경우 빌드 계층에만 별도 한도를 적용하고 운영 guest 전체 한도를 추가로 낮추지 않는다.
2. **빌드 시작 전 host/guest headroom을 검사하고 대기시킨다.** 현재 세 guest hard cap 합계는 약 36.5GiB로 물리 RAM 약 31.2GiB보다 크다. 이는 Incus 한도 오작동이나 예약 RAM을 뜻하지 않지만 동시 최대 사용을 보장하지도 않는다. 호스트 4GiB 여유 및 guest soft ceiling을 기준으로 새 비필수 빌드를 대기시키는 연결이 필요하다. 현재 Healer의 복구 headroom 검사만으로 모든 빌드가 제어되지는 않는다.
3. **캐시 회수와 I/O를 함께 본 뒤 musecat soft limit을 재평가한다.** 약 1시간에 직접 scan 721,498회, file refault 455,537회가 증가했다. 페이지 단위 카운터이며 실제 누수 바이트가 아니다. `memory.high=10GiB` 근처에서 캐시 회수가 배포 I/O를 증폭시킬 가능성이 있다. 운영 guest의 RAM을 더 낮추거나 캐시를 강제 삭제하기 전에 빌드 분리 전후의 PSI/refault/누적 쓰기를 비교한다. 이번 검토에서 한도를 올리거나 내리지 않았다.
4. **24시간 쓰기 증가량을 역할별로 비교한다.** 배포 횟수와 cgroup 쓰기 delta, SSD SMART 누적 쓰기 증가, Redis/DB 지속 쓰기, Docker 로그 증가를 나눠 본다. 한 시간의 배포 burst를 일일 수명 소모로 단순 외삽하지 않는다. Redis 영속성 비활성화나 무차별 Docker prune은 이번 변경에 포함하지 않는다.

memory.high는 OOM을 직접 발생시키는 hard cap이 아니라 회수·속도 제한의 경계다. 관련 동작과 writeback 귀속은 [Linux cgroup v2 문서](https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html)를 따른다.

## 다시 확인하기

```sh
journalctl -u coolify-fixkit-host.service --since '1 hour ago' -o cat
journalctl -u coolify-fixkit-discord.service --since '1 hour ago' -o cat
journalctl -k --since '90 minutes ago' --grep 'oom-kill|Out of memory|Killed process|I/O error|resetting link'
python3 /usr/local/lib/coolify-fixkit/host_guard.py --policy /etc/coolify-fixkit/host-policy.json --once
```

Debian root에서 실행한다. 마지막 명령은 관찰만 하며 `--apply`가 없으므로 정책 값을 쓰지 않는다. 토큰 파일이나 애플리케이션 환경변수를 출력할 필요는 없다.
