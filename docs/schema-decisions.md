# 정산 테이블 구조

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

청크를 처리할 때 아래 세 변경을 하나의 트랜잭션으로 묶어야 합니다. 정산 프로그램은 아직 구현 전입니다.

```mermaid
flowchart LR
    A[결과 저장] --> B[지갑 잔액 증가] --> C[청크 완료 기록] --> D[COMMIT]
    subgraph T[청크별 트랜잭션]
        A
        B
        C
    end
```

제약과 권한의 이유는 [schema.sql](../sql/schema.sql) 주석에 정리했습니다. 중복·잘못된 값·권한 위반은 DB에서 확인했습니다. 참조 ID 확인과 동시 실행·장애 복구 검증은 다음 단계입니다.
