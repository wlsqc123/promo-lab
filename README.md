# PostgreSQL 정산 실험 개발 환경

개인 DBA 포트폴리오 실험용 환경입니다. 정산 로직과 실험 시나리오는 다음 단계에서 구현합니다.

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

`sql/schema.sql`은 6개 테이블을 하나의 트랜잭션으로 생성합니다. PK·UNIQUE·NOT NULL·CHECK를 구성하며 FK는 사용하지 않습니다. 정산·길드 ID는 데이터 준비와 결과 확인 과정에서 검사할 예정입니다. score_event에는 PK 인덱스만 둡니다.

점수·지갑 잔액은 0 이상, 보상은 `total_score / 100`과 일치해야 합니다. 0 이상인 정수의 나눗셈이므로 설계서의 FLOOR와 같습니다. 정산 상태는 DRAFT·READY·COMPLETED입니다. READY부터 입력 확정 시각이 필요하며, 완료 시각은 COMPLETED 상태에서만 기록합니다. 청크 번호와 시작·끝 ID는 양수입니다. 시작·끝 ID의 길드가 실제로 존재할 필요는 없습니다.

| 계정 | 권한 |
|---|---|
| lab_admin | DDL, 데이터 적재·초기화, 계측·장애 실험 |
| lab_worker | 6개 테이블 SELECT, 결과 INSERT, 지갑 balance UPDATE, job state·completed_at UPDATE, 청크 completed_at UPDATE |

작업자는 입력 수정·결과 덮어쓰기·삭제·처리 범위 변경·테이블 생성을 할 수 없습니다. 관리자도 READY 이후에는 입력을 수정하지 않아야 합니다. 대상 길드 누락과 전체 처리 완료 여부는 프로그램에서 확인할 예정입니다.

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

실험 중 확정된 job·guild는 삭제하지 않습니다. ID 확인은 아직 구현 전이므로 현재 DB는 존재하지 않는 정산·길드 ID도 허용합니다. 설계 판단은 [스키마 설계 근거](docs/schema-decisions.md)에 정리했습니다.

## 다음 구현

고정 seed로 데이터·기대값 생성 → 단일 정산·검증 → 동시 실행·장애 주입 → 측정 순으로 진행합니다. Redis는 핵심 실험 완료 후 추가합니다.
