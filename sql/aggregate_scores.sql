-- 같은 구조의 테이블을 스키마로 구분. 비교 중 SQL 형태와 조건은 고정.
SELECT guild_id, SUM(score)
FROM {schema}.score_event
WHERE job_id = %s AND guild_id BETWEEN %s AND %s
GROUP BY guild_id
ORDER BY guild_id;
