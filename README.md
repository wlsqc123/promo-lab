# PostgreSQL 정산 실험 개발 환경

개인 DBA 포트폴리오 실험용 환경입니다. 정산 로직과 실험 시나리오는 다음 단계에서 구현합니다.

## 시작

Windows에서 Docker Desktop을 실행하고 Linux 컨테이너 모드를 사용합니다. 호스트 Python 설치는 필요 없습니다. 이 폴더에서 PowerShell로 실행합니다.

```powershell
docker compose up -d --wait db
docker compose run --rm --build runner doctor
```

doctor는 실제 DB 연결, PostgreSQL 18, UTC, READ COMMITTED, pg_stat_statements 조회 및 임시 테이블 쓰기를 확인합니다. 결과는 `results/environment.json`에 저장됩니다.

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

버전 고정은 실험 재현을 위한 선택입니다. 변경하면 결과에 버전과 digest를 함께 기록합니다. Docker Desktop에 충분한 메모리를 할당합니다. 실제 PC 사양과 Docker 버전·리소스 제한은 성능 측정 시 별도로 기록해야 합니다.

DB는 named volume에 보존됩니다. 초기화 SQL은 빈 볼륨의 첫 시작에만 실행됩니다.

## 테이블과 권한

`sql/schema.sql`은 6개 테이블을 하나의 트랜잭션으로 생성합니다. ID는 양수 BIGINT를 명시적으로 입력합니다. 시퀀스는 사용하지 않습니다. PK·UNIQUE·NOT NULL·CHECK를 구성하며 FK는 사용하지 않습니다. 참조 확인은 후속 Python 생성기·검증기에 맡깁니다. score_event에는 PK 인덱스만 둡니다.

점수·지갑은 0 이상, 보상은 `total_score / 100`과 일치해야 합니다. 비음수 정수 나눗셈이므로 설계서의 FLOOR와 같습니다. job 상태는 DRAFT·READY·COMPLETED이며 READY부터 cutoff가 필요하고 COMPLETED에서만 완료 시각이 존재합니다. 청크 번호와 범위 경계는 양수이며, 범위 경계에 실제 길드가 존재할 필요는 없습니다.

| 계정 | 권한 |
|---|---|
| lab_admin | DDL, 데이터 적재·초기화, 계측·장애 실험 |
| lab_worker | 6개 테이블 SELECT, 결과 INSERT, 지갑 balance UPDATE, job state·completed_at UPDATE, 청크 completed_at UPDATE |

작업자는 입력 수정·결과 덮어쓰기·삭제·청크 범위 변경·DDL을 할 수 없습니다. READY 이후 입력 불변은 적재 프로그램과 관리자 사용 절차에서도 지켜야 합니다. CHECK는 다른 행을 검사하지 않으므로 청크 중복 범위, 대상 목록 확정, job 상태 전이 순서와 전체 완료 여부는 후속 생성기·정산기·검증기가 검사합니다. BIGINT 범위를 넘는 합계는 실패로 처리하며 생성기에서 데이터 규모를 제한합니다.

기존 환경에 스키마를 한 번 적용할 때는 다음 명령을 사용합니다. 이미 생성된 테이블에는 재적용하지 않습니다.

```powershell
docker compose up -d --wait db
docker compose exec -T db psql -U lab_admin -d settlement_lab -v ON_ERROR_STOP=1 -f /lab/sql/schema.sql
```

다음 검증은 작은 데이터를 넣어 제약과 실제 작업자 권한을 검사한 뒤 모두 롤백합니다. 데이터가 없는 개발 DB에서 실행합니다.

```powershell
docker compose exec -T db psql -U lab_admin -d settlement_lab -v ON_ERROR_STOP=1 -f /lab/sql/check_schema.sql
```

다음 명령은 **이 프로젝트의 DB 데이터를 모두 삭제**합니다. 초기 상태로 실험을 다시 시작할 때만 사용합니다.

```powershell
docker compose down -v
docker compose up -d --wait db
```

## FK가 있던 기존 DB 변경

기존 DB에는 다음 마이그레이션을 한 번 적용합니다. 데이터 삭제 없이 FK를 제거하고 양수 ID 검사를 추가합니다. 새 볼륨은 schema.sql에 반영돼 있으므로 이 명령이 필요 없습니다.

```powershell
docker compose exec -T db psql -U lab_admin -d settlement_lab -v ON_ERROR_STOP=1 -f /lab/sql/migrations/001_remove_foreign_keys.sql
```

실험 중 확정된 job·guild는 삭제하지 않습니다. 생성기·검증기는 아직 구현 전이므로 현재 DB는 고아 데이터를 차단하지 않습니다. 설계 판단은 [스키마 설계 근거](docs/schema-decisions.md)에 정리했습니다.

## 다음 구현

고정 seed·독립 기대값 생성 → 단일 정산·검증 → 동시 실행·장애 주입 → 측정 순으로 진행한다. Redis는 핵심 실험 완료 후 추가합니다.
