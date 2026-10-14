# 정산 구조

## 정산과 작업자

정산(job)은 특정 시즌·날짜에 처리할 작업 1건입니다. 청크는 대상 길드를 나눈 처리 단위이며, 작업자는 정산 프로그램을 실행하는 프로세스입니다. 여러 작업자가 같은 job을 처리하며, 별도의 정산 담당 프로세스는 두지 않습니다.

현재 작업자는 같은 청크 목록을 번호순으로 읽습니다. 청크를 미리 나눠 배정하지 않고, DB의 행 잠금과 완료 기록으로 중복 처리를 막는 방식입니다. 이는 병렬 분담이 아니라 같은 정산이 중복 실행되는 상황을 확인하는 실험입니다. [작업자 2·4·8개 실행 결과](duplicate-execution.md)를 기록했습니다.

```mermaid
sequenceDiagram
    participant W1 as 작업자 1
    participant DB as PostgreSQL · 같은 job
    participant W2 as 작업자 2
    W1->>DB: 청크 1 잠금 획득
    W2->>DB: 청크 1 잠금 요청
    Note over W2,DB: 작업자 1의 트랜잭션 종료까지 대기
    W1->>DB: 결과 저장 · 잔액 증가 · 청크 완료 기록
    W1->>DB: COMMIT · 잠금 해제
    DB-->>W2: 잠금 획득 · 완료 기록 확인
    Note over W2: 완료된 청크는 건너뛰기
```

서로 다른 청크는 동시에 처리할 수 있습니다. 같은 청크는 먼저 잠금을 잡은 작업자가 처리하고, 기다린 작업자는 잠금을 얻은 뒤 완료 여부를 다시 확인합니다. 앞선 처리가 롤백돼 완료 기록이 없다면 기다린 작업자가 처리합니다.

## 테이블 관계

점선은 ID로 연결되는 논리적 관계입니다. 실제 FK는 없으며, 주요 컬럼만 표시했습니다.

```mermaid
erDiagram
    guild ||..o{ score_event : guild_id
    guild ||..o{ settlement_result : guild_id
    guild ||..o| guild_wallet : guild_id
    settlement_job ||..o{ score_event : job_id
    settlement_job ||..o{ settlement_result : job_id
    settlement_job ||..o{ settlement_chunk : job_id

    guild {
        bigint guild_id PK
        text name
    }
    settlement_job {
        bigint job_id PK
        bigint season_id "시즌·날짜 조합 UNIQUE"
        date settlement_date
        text state "DRAFT / READY / COMPLETED"
    }
    score_event {
        bigint event_id PK
        bigint job_id
        bigint guild_id
        bigint score "입력 점수"
    }
    settlement_result {
        bigint job_id PK "길드 ID와 복합 PK"
        bigint guild_id PK
        bigint total_score "점수 합계"
        bigint reward_points "합계 / 100"
    }
    guild_wallet {
        bigint guild_id PK
        bigint balance "누적 잔액"
    }
    settlement_chunk {
        bigint job_id PK "청크 번호와 복합 PK"
        bigint chunk_no PK
        bigint first_guild_id "처리 시작 ID"
        bigint last_guild_id "처리 끝 ID"
        timestamptz completed_at "처리 전 NULL"
    }
```

## 청크 처리

청크 행을 잠근 뒤 완료 여부를 확인합니다. 미완료 청크는 아래 세 변경을 하나의 트랜잭션으로 처리합니다.

```mermaid
flowchart LR
    A[결과 저장] --> B[지갑 잔액 증가] --> C[청크 완료 기록] --> D[COMMIT]
    subgraph T[청크별 트랜잭션]
        A
        B
        C
    end
```

제약과 권한의 이유는 [schema.sql](../sql/schema.sql) 주석에 정리했습니다. 단일 정산·재실행·동시 중복 실행, [작업자 종료 후 재실행](termination.md), [락 시간 초과·데드락 재시도](locking.md)를 확인했습니다.
