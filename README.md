# PostgreSQL 정산 실험 개발 환경

개인 DBA 포트폴리오 실험용 환경입니다. 현재는 작은 데이터의 단일 정산과 재실행을 확인할 수 있습니다.

길드원 최대 40명이 점수를 갱신하는 실무 환경에서의 고민을 바탕으로 합니다. 확정된 입력의 정산과 별도 테이블의 동시 점수 갱신을 확인했습니다. [실험 배경과 구조](docs/schema-decisions.md)를 정리했습니다.

## 시작

Windows에서 Docker Desktop을 실행하고 Linux 컨테이너 모드를 사용합니다. Python을 PC에 따로 설치할 필요는 없습니다. 이 폴더에서 PowerShell을 열고 다음 명령을 실행합니다.

```powershell
docker compose up -d --wait db
docker compose run --rm --build runner doctor
```

doctor는 DB 연결, PostgreSQL 18 버전, UTC 설정, READ COMMITTED 격리 수준, pg_stat_statements 조회, 임시 테이블 읽기·쓰기를 확인합니다. 결과는 `results/environment.json`에 저장됩니다.

## DB 접속

DB 클라이언트에서 host `localhost`, port `15432`, database `settlement_lab`을 사용합니다. 관리자 계정은 `lab_admin / local_lab_admin`, 작업자 계정은 `lab_worker / local_lab_worker`입니다. 로컬 실험 전용 비밀번호이며 공개 서버에 사용하지 않습니다. 포트는 로컬 PC에서만 접근할 수 있습니다.

```powershell
docker compose exec db psql -U lab_admin -d settlement_lab
docker compose logs --tail 100 db
docker compose stop
docker compose start db
```

## 고정된 환경

- PostgreSQL 18.6 Bookworm 및 Python 3.13.7 slim Bookworm: 이미지 digest 고정
- psycopg와 binary 패키지: 3.3.6 고정
- DB: CPU 최대 2개, 메모리 최대 2 GiB, 공유 메모리 256 MiB
- DB·Python: UTC, 작업자: READ COMMITTED
- 작업자 lock_timeout 2초, statement_timeout 30초
- pg_stat_statements, log_lock_waits, track_io_timing 활성화

같은 환경에서 실험을 반복할 수 있도록 버전을 고정했습니다. 변경하면 결과에 버전과 digest를 함께 기록합니다. Docker Desktop에 충분한 메모리를 할당합니다. 실제 PC 사양과 Docker 버전·리소스 제한은 성능 측정 시 별도로 기록해야 합니다.

DB 데이터는 Docker 볼륨에 저장됩니다. 초기화 SQL은 빈 볼륨의 첫 시작에만 실행됩니다.

## 테이블과 권한

`sql/schema.sql`은 6개 테이블을 하나의 트랜잭션으로 생성합니다. PK·UNIQUE·NOT NULL·CHECK를 구성하며 FK는 사용하지 않습니다. 데이터 준비 시 정산·길드 ID를 지정하고, verify에서 준비한 목록과 비교합니다. score_event에는 PK 인덱스만 둡니다.

점수·지갑 잔액은 0 이상, 보상은 `total_score / 100`과 일치해야 합니다. 0 이상인 정수의 나눗셈이므로 설계서의 FLOOR와 같습니다. 정산 상태는 DRAFT·READY·COMPLETED입니다. READY부터 입력 확정 시각이 필요하며, 완료 시각은 COMPLETED 상태에서만 기록합니다. 청크 번호와 시작·끝 ID는 양수입니다. 시작·끝 ID의 길드가 실제로 존재할 필요는 없습니다.

| 계정 | 권한 |
|---|---|
| lab_admin | DDL, 데이터 적재·초기화, 계측·장애 실험 |
| lab_worker | 6개 테이블 SELECT, 결과 INSERT, 지갑 balance UPDATE, job state·completed_at UPDATE, 청크 completed_at UPDATE |

작업자는 입력 수정·결과 덮어쓰기·삭제·처리 범위 변경·테이블 생성을 할 수 없습니다. 관리자도 READY 이후에는 입력을 수정하지 않아야 합니다. 대상 길드 목록과 전체 처리 완료 여부는 verify에서 확인합니다.

이미 실행 중인 DB에 아직 테이블이 없다면 다음 명령을 사용합니다. 이미 생성된 테이블에는 재적용하지 않습니다.

```powershell
docker compose up -d --wait db
docker compose exec -T db psql -U lab_admin -d settlement_lab -v ON_ERROR_STOP=1 -f /lab/sql/schema.sql
```

다음 명령은 소량의 데이터를 넣어 제약과 작업자 권한을 확인한 뒤 롤백합니다. 데이터가 없는 개발 DB에서 실행합니다.

```powershell
docker compose exec -T db psql -U lab_admin -d settlement_lab -v ON_ERROR_STOP=1 -f /lab/sql/check_schema.sql
```

다음 명령은 **이 프로젝트의 DB 데이터를 모두 삭제**합니다. 초기 상태로 실험을 다시 시작할 때만 사용합니다.

```powershell
docker compose down -v
docker compose up -d --wait db
```

## FK가 있던 기존 DB 변경

FK가 있던 기존 DB에는 다음 변경 SQL을 한 번 실행합니다. 데이터 삭제 없이 FK를 제거하고 양수 ID 검사를 추가합니다. 새 볼륨에는 변경된 schema.sql이 적용되므로 이 명령이 필요 없습니다.

```powershell
docker compose exec -T db psql -U lab_admin -d settlement_lab -v ON_ERROR_STOP=1 -f /lab/sql/migrations/001_remove_foreign_keys.sql
```

실험 중 확정된 job·guild는 삭제하지 않습니다. DB에는 FK가 없어 존재하지 않는 정산·길드 ID도 허용하지만, verify는 준비한 목록과 다른 데이터를 오류로 처리합니다. 설계 판단은 [스키마 설계 근거](docs/schema-decisions.md)에 정리했습니다.

## 작은 데이터로 정산 실행

현재는 비어 있는 DB에 정산 1개를 준비합니다. 기본값은 seed 42, 길드 100개, 이벤트 1만 개, 청크당 길드 10개입니다. 마지막 길드는 이벤트 없이 준비해 0점 결과도 확인합니다.

```powershell
docker compose run --rm --build runner prepare
docker compose run --rm runner settle
docker compose run --rm runner verify
docker compose run --rm runner settle
docker compose run --rm runner verify
```

prepare는 관리자 계정으로 데이터를 넣고, settle·verify는 작업자 계정을 사용합니다. 예상 점수·보상은 DB에 넣기 전에 Python에서 계산해 `results/job-1/expected.json`에 저장합니다. 완료된 정산은 길드별 결과·잔액·입력·청크 목록을 비교합니다.

실행 기록은 `results/job-1/`에 저장됩니다. 재실행 후 처리한 청크가 0개이고 두 verify 기록의 `data_hash`가 같으면 결과와 잔액, 완료 기록이 유지된 것입니다. 해시 비교와 함께 모든 길드의 값이 예상값과 같은지도 확인합니다.

데이터나 예상값 파일이 이미 있으면 prepare는 중단합니다. 기존 데이터를 덮어쓰지 않습니다. 데이터를 다시 준비하려면 DB와 예상값 파일을 함께 별도로 보관한 뒤 초기화해야 합니다. 준비 도중 실패한 경우 예상값 파일이 남을 수 있으므로 DB 상태부터 확인합니다.

완료된 기본 데이터에서 다음 명령으로 오류 감지와 청크 롤백도 확인할 수 있습니다. 임시 변경은 모두 롤백합니다.

```powershell
docker compose run --rm --entrypoint python runner -m runner.check_single
```

2026-10-14에 기본 데이터의 단일 정산과 재실행, 잘못된 잔액·결과 누락·입력 변경 감지, 중복 결과 거부와 지갑 누락 시 청크 롤백을 확인했습니다. 처리 오류는 실패로 종료하며, 데드락·락 시간 초과·직렬화 실패는 최초 시도 외 최대 3회 재시도합니다. 연결 종료 후 COMMIT 성공 여부를 확인하는 기능은 아직 구현 전입니다.

## 같은 정산을 동시에 실행

다음 명령은 별도 DB에 기본 데이터를 준비하고, 같은 정산을 여러 프로세스에서 동시에 실행합니다. 기존 DB는 변경하지 않으며 실험 DB는 종료 시 제거합니다. 결과 파일은 `results/duplicate-*`에 남깁니다.

```powershell
docker compose run --rm --build runner duplicate --workers 2
docker compose run --rm runner duplicate --workers 4
docker compose run --rm runner duplicate --workers 8
```

잠금 대기 관측, 길드별 결과·잔액 비교, 완료 후 재실행 확인 중 하나라도 실패하면 명령이 실패합니다. [실행 결과](docs/duplicate-execution.md)를 정리했습니다.

## 작업자 강제 종료 후 재실행

첫 청크의 커밋 전·직후에 작업자를 강제 종료하고, 별도 실험 DB에서 종료 후 상태와 재실행 결과를 확인합니다. 기존 DB는 변경하지 않습니다.

```powershell
docker compose run --rm --build runner terminate --point before-commit
docker compose run --rm runner terminate --point after-commit
```

실행 기록은 `results/termination-*`에 저장됩니다. [실험 결과](docs/termination.md)에 종료 지점과 복구 결과를 정리했습니다.

## 락 시간 초과와 데드락

별도 DB에서 지갑을 잠가 시간 초과·데드락을 만들고, 같은 순서로 잠근 경우와 비교합니다.

```powershell
docker compose run --rm --build runner locking --scenario timeout
docker compose run --rm runner locking --scenario deadlock
docker compose run --rm runner locking --scenario ordered
```

SQLSTATE·재시도·롤백과 최종 결과를 확인합니다. 로그와 결과는 `results/locking-*`에 저장되며, [실험 결과](docs/locking.md)를 정리했습니다.

## 유저·길드 점수 동시 갱신

40개 연결로 유저별 점수와 길드 공용 score를 같은 트랜잭션에서 갱신합니다. 한 길드에 집중한 경우와 네 길드로 나눈 경우를 별도 DB에서 비교합니다.

```powershell
docker compose run --rm --build runner score-updates
```

기본값은 유저당 50회 요청입니다. 각각 워밍업 1회·측정 5회를 실행하며, `--requests-per-user`로 요청 수를 바꿀 수 있습니다. 기존 데이터는 유지하고, 기록은 `results/score-updates-*`에 저장합니다. [측정 결과와 조건](docs/score-updates.md)을 정리했습니다.

## 점수 집계 인덱스 비교

같은 데이터로 PK만 있는 상태, 복합 인덱스, score를 포함한 커버링 인덱스를 비교합니다. 별도 DB에 세 테이블을 만들며 기존 데이터는 유지합니다.

```powershell
docker compose run --rm --build runner indexes
```

기본값은 길드 1만 개·정산 4개·구성별 이벤트 100만 개입니다. `--events`로 구성별 이벤트 수를 바꿀 수 있습니다. 조회는 워밍업 1회·측정 5회, 적재 비용은 1회 기록합니다. 입력·예상값·실행계획·CSV는 `results/indexes-*`에 저장합니다. [실행계획과 비용 비교](docs/indexes.md)를 정리했습니다.
