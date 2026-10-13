# PostgreSQL 정산 실험 개발 환경

개인 DBA 포트폴리오 실험용 환경이다. 정산 로직과 실험 시나리오는 다음 단계에서 구현한다.

## 시작

Windows에서 Docker Desktop을 실행하고 Linux 컨테이너 모드를 사용한다. 호스트 Python 설치는 필요 없다. 이 폴더에서 PowerShell로 실행한다.

```powershell
docker compose up -d --wait db
docker compose run --rm --build runner doctor
```

doctor는 실제 DB 연결, PostgreSQL 18, UTC, READ COMMITTED, pg_stat_statements 조회 및 임시 테이블 쓰기를 확인한다. 결과는 `results/environment.json`에 저장된다.

## DB 접속

DB 클라이언트에서 host `localhost`, port `15432`, database `settlement_lab`을 사용한다. 관리자 계정은 `lab_admin / local_lab_admin`, 작업자 계정은 `lab_worker / local_lab_worker`이다. 로컬 실험 전용 비밀번호이며 공개 서버에 사용하지 않는다. 포트는 로컬 PC에서만 접근할 수 있다.

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

버전 고정은 실험 재현을 위한 선택이다. 변경하면 결과에 버전과 digest를 함께 기록한다. Docker Desktop에 충분한 메모리를 할당한다. 실제 PC 사양과 Docker 버전·리소스 제한은 성능 측정 시 별도로 기록해야 한다.

DB는 named volume에 보존된다. 초기화 SQL은 빈 볼륨의 첫 시작에만 실행된다. 작업자에게는 아직 업무 테이블 권한을 부여하지 않았다. DDL 구현 시 score_event 읽기 권한과 정산 테이블 쓰기 권한을 구분한다.

다음 명령은 **이 프로젝트의 DB 데이터를 모두 삭제**한다. 초기 상태로 실험을 다시 시작할 때만 사용한다.

```powershell
docker compose down -v
docker compose up -d --wait db
```

## 다음 구현

DDL → 고정 seed·독립 기대값 생성 → 단일 정산·검증 → 동시 실행·장애 주입 → 측정 순으로 진행한다. Redis는 핵심 실험 완료 후 추가한다.
