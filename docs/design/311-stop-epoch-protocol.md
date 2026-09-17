# Design note — DB-backed STOP epoch protocol (agent_crew#314, incident alfred#39)

남은 3 blocker(P0-1 원자성 / B3-b result-carrying / B4-b crash-safe replay)를 **하나의 프로토콜**로 묶는다.
원칙: **원자적 STOP 판단의 최종 authority는 DB 안에 있다.** `pause.json`은 부팅/외부 제어 신호로 남되
claim/enqueue 원자 결정의 단독 근거가 될 수 없다.

## 1. Authoritative STOP epoch 저장 + pause.json 동기화
- 새 테이블 `runtime_stop(id=1 단일행)`: `epoch INTEGER, paused INTEGER(0/1), incident TEXT, updated_at REAL`.
  - `epoch`는 monotonic — pause/resume 전이마다 +1.
- **동기화**: `crew pause`/`resume`(CLI)는 (a) `pause.json`(외부 durable/boot 신호) 기록 후 (b) `TaskQueue.set_stop_epoch(paused, epoch, incident)`로 DB 행을 같은 값으로 기록.
- **부팅 reconciliation(fail-closed)**: 서버/큐 init 시
  `effective_paused = pause.json.paused OR db.paused`,
  `effective_epoch  = max(pause.json.generation, db.epoch)`.
  둘 중 하나라도 paused면 paused. 화해값을 양쪽에 다시 기록. (pause.json 외부 편집/DB 손상 어느 쪽도 STOP을 약화 못함.)

## 2. Transaction boundary (모두 같은 DB lock 도메인)
- **enqueue**: `BEGIN IMMEDIATE; SELECT runtime_stop(같은 txn); paused면 ROLLBACK+raise PausedError; 아니면 INSERT; COMMIT`.
  → STOP write(별도 `BEGIN IMMEDIATE; UPDATE runtime_stop; COMMIT`)가 이 txn의 INSERT 전에 commit되면 SQLite write 직렬화로 **반드시 보인다**. file-read TOCTOU 제거.
- **dequeue(normal)**: `BEGIN IMMEDIATE; SELECT runtime_stop; paused면 ROLLBACK+return None; 아니면 SELECT pending+UPDATE→in_progress; COMMIT`.
- **dequeue_discuss_for_agent**: 동일 패턴(같은 txn 내 stop 확인).
- **result-driven successor 생성**: 전부 enqueue를 거치므로 위 원자 게이트에 자동 포함. 추가로 submit_result 상단 fast-path 체크 + 억제기록(아래).
- `pause.json` in-txn 읽기는 보조(빠른 fail)로만. **권위는 DB 행.**

## 3. Suppressed-result schema / transition identity
- 새 테이블 `suppressed_cascade`:
  `parent_task_id TEXT PRIMARY KEY, task_type TEXT, result_json TEXT NOT NULL, stop_epoch INTEGER,
   state TEXT CHECK in ('pending','replaying','applied'), created_at REAL, updated_at REAL`.
- 억제 결정과 **같은 txn**에서 기록. `result_json` = 원 제출 result 전체(**절대 'unknown' placeholder 금지** — replay 재현 가능해야).
- transition identity = `parent_task_id`(PK) → parent당 1행(dedup). enqueue race의 PausedError 경로도
  result를 갖고 이 행을 upsert(예외 핸들러가 아니라 submit_result 내 result-scope에서).

## 4. Replay state machine + crash points
- 상태: `pending → replaying → applied`.
- `POST /admin/replay-suppressed`(unpaused일 때만):
  1. 각 행을 **CAS** `UPDATE suppressed_cascade SET state='replaying' WHERE parent_task_id=? AND state='pending'` — rowcount=1인 claimer만 진행(동시 replay dedup).
  2. `result_json`으로 production `submit_result` 재실행. successor는 **결정론적 task_id**(예: `review-<parent>`)로 enqueue → task_id PK dedup으로 idempotent.
  3. 성공 시 CAS `replaying → applied`.
- crash 지점:
  - (a) `pending→replaying` 후 successor 생성 전 crash → 부팅 reconcile이 `replaying` 행 재시도. successor task_id 결정론적이라 재실행=at-most-once.
  - (b) successor 생성 후 `→applied` 전 crash → 재시도 시 enqueue가 PK 충돌로 no-op → `applied` 마킹. 중복 successor 없음.
- 따라서 **결정론적 successor id + 상태머신 = crash-safe at-most-once**.

## 5. 직접 외부 mutation(merge)의 idempotency/receipt
- merge는 DB txn 공유 불가(외부 gh 호출). stable op key + durable receipt:
  - 새 테이블 `external_op(op_key TEXT PRIMARY KEY, state TEXT('pending','done'), pr_number INTEGER, at REAL)`.
  - merge 전: `INSERT OR IGNORE external_op(op_key='merge:pr:<n>','pending')`. 이미 있으면(pending/done) **skip**(check-then-act 아님, PK 원자성).
  - merge 실행 → `UPDATE ... SET state='done'`.
  - crash(merge 후 done 전) → 재시도가 op_key 존재를 보고 PR 실제 merge 상태를 gh로 재확인 후 done. gh는 이미-merged에 대해 graceful 실패.
  - 추가로 pause 게이트(이미 반영된 P0-2b)로 STOP 중 merge 미시작.

## 6. Migration / bootstrap
- `TaskQueue` init에서 `CREATE TABLE IF NOT EXISTS runtime_stop/suppressed_cascade/external_op`.
- `runtime_stop`이 없으면 default(unpaused, epoch 0) 후 pause.json과 reconcile(§1).
- 기존 DB: 테이블만 추가(기존 tasks 무변경). restart-while-paused: pause.json paused → boot sync가 runtime_stop paused 기록 → 첫 요청부터 게이트. crash 전 `replaying` 행 → boot reconcile 재실행.

## Deterministic tests (필수)
- **interleaving**: pre-check는 unpaused를 보지만, in-txn SELECT 직전 DB epoch을 paused로 advance → 같은 DB 도메인에서 INSERT/claim이 **rejected**. (DB 행을 직접 advance해 결정론적으로 재현.)
- **enqueue 원자 거부**: runtime_stop paused → enqueue PausedError.
- **replay crash/retry**: `replaying` 상태 행을 남긴 뒤 replay 재실행 → successor **at-most-once**(결정론 id dedup), `applied`로 수렴.
- **merge receipt**: `external_op` done 상태에서 재요청 → merge 미재실행.
- 회귀: 기존 pause 스위트 + 신규. 로컬 테스트는 증거이며 CI 아님.

## 구현 순서
runtime_stop 테이블+set/get_stop_epoch(§1) → enqueue/dequeue/discuss in-txn 게이트(§2) → cli pause/resume가 DB 동기화 → suppressed_cascade 테이블+억제 기록(§3) → replay 상태머신(§4) → external_op merge receipt(§5) → migration/bootstrap(§6) → deterministic 테스트.
