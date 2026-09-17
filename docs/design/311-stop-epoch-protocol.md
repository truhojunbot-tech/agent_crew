# Design note v2 — DB-backed STOP epoch protocol (agent_crew#314, incident alfred#39)

남은 3 blocker(P0-1 원자성 / B3-b result-carrying / B4-b crash-safe replay)를 **하나의 프로토콜**로 묶는다.
원칙: **원자적 STOP 판단의 최종 authority는 DB 안에 있다.** `pause.json`은 부팅/외부 제어 신호로 남되
claim/enqueue 원자 결정의 단독 근거가 될 수 없다.

> v2 개정(호준님 독립리뷰 반영): (1) DB epoch가 linearization point — pause.json은 DB를 미러링만.
> (2) cascade outbox는 "막혔을 때만"이 아니라 result 저장과 **항상 같은 txn**. (3) replay는 lease/owner
> reclaim + stable transition key dedup, merge는 reservation+상태재확인+idempotent reconciliation.
> (4) #41 post-restart ACK는 새 build가 DB stop epoch/incident를 읽고 paused로 올라왔는지까지 확인.

## 1. Authoritative STOP epoch — DB가 linearization point, pause.json은 미러

- 새 테이블 `runtime_stop(id=1 단일행)`: `epoch INTEGER, paused INTEGER(0/1), incident TEXT, updated_at REAL`.
  - `epoch`는 monotonic — pause/resume 전이마다 +1. **STOP의 유일한 linearization point는 이 행의 commit이다.**
- **쓰기 순서(중요, v1에서 반대였음)**: `crew pause`/`resume`는
  1. `BEGIN IMMEDIATE; SELECT epoch FROM runtime_stop; new_epoch = epoch+1;
     UPDATE runtime_stop SET paused=?, epoch=new_epoch, incident=?, updated_at=?; COMMIT` — **DB에서 epoch를 먼저 할당·commit**.
  2. commit 성공 후에만 `pause.json`을 **그 값 그대로 미러링** 기록(부팅/외부 관측용 durable 신호).
  - pause.json 쓰기 실패는 STOP 판단에 영향 없음(권위는 이미 DB에 commit). 반대로 pause.json만 있고 DB 미반영인 상태는 부팅 reconcile이 흡수.
- **부팅 reconciliation(단순 OR 아님 — 더 높은 epoch가 승자, 불일치는 fail-closed)**:
  - `db = runtime_stop 행`, `pj = pause.json`.
  - `winner = argmax(epoch)`(db.epoch vs pj.generation). 승자의 `paused`/`incident`를 채택.
  - **같은 epoch인데 paused/incident가 서로 다르면** → 화해 불가 → **fail-closed로 paused 유지 + 부팅 차단(사람 개입 필요)**. (조용히 OR로 뭉개지 않는다.)
  - 어느 쪽이든 손상/파싱불가 → 그쪽을 epoch=-1(무효)로 보고 상대를 승자로 두되, 상대도 paused가 아니면 fail-closed.
  - 화해 결과를 DB와 pause.json 양쪽에 다시 기록(수렴).

## 2. Transaction boundary (모두 같은 DB lock 도메인, 권위=DB 행)

- **enqueue**: `BEGIN IMMEDIATE; SELECT paused,epoch FROM runtime_stop(같은 txn);
  paused면 ROLLBACK+raise PausedError; 아니면 INSERT task; COMMIT`.
  → STOP write(§1의 `BEGIN IMMEDIATE; UPDATE runtime_stop; COMMIT`)가 이 txn의 SELECT 전에 commit되면
  SQLite write 직렬화로 **반드시 보인다**. file-read TOCTOU 제거.
- **dequeue(normal)**: `BEGIN IMMEDIATE; SELECT paused FROM runtime_stop; paused면 ROLLBACK+return None;
  아니면 SELECT pending+UPDATE→in_progress; COMMIT`.
- **dequeue_discuss_for_agent**: 동일 패턴(같은 txn 내 runtime_stop 확인).
- **result-driven successor 생성**: 전부 enqueue를 거치므로 위 원자 게이트에 자동 포함(§3의 outbox executor가 enqueue 호출).
- `pause.json` in-txn 읽기는 **제거**. 권위는 전적으로 DB 행. (v1의 "보조 fast-fail" 문구 삭제 — 이중 소스 혼란 방지.)

## 3. Result 저장과 cascade outbox — 항상 같은 txn (막혔을 때만 아님)

v1 결함: "막혔을 때만 suppressed_cascade 기록"은 `result 저장 → crash → suppression 기록 전`
구간에서 continuation 유실. 그래서 **TaskResult 저장과 outbox 생성을 항상 같은 txn에서 수행**한다.

- 새 테이블 `cascade_outbox`:
  `parent_task_id TEXT PRIMARY KEY, task_type TEXT, result_json TEXT NOT NULL, stop_epoch INTEGER,
   attempt_id TEXT, lease_owner TEXT, lease_expires_at REAL,
   state TEXT CHECK in ('pending','replaying','applied'), created_at REAL, updated_at REAL`.
- **submit_result 경로**: `BEGIN IMMEDIATE;`
  1. TaskResult(결과) 저장(기존 로직).
  2. **동일 txn**에서 `INSERT INTO cascade_outbox(parent_task_id, task_type, result_json=원 result 전체, stop_epoch=현재 runtime_stop.epoch, state='pending')`.
     - `result_json`은 **원 제출 result 전체**(절대 'unknown' placeholder 금지 — replay 재현 가능해야).
     - pause 여부와 무관하게 항상 outbox에 넣는다. (즉 outbox = cascade 실행 대기열의 단일 소스.)
  3. `COMMIT`.
- **pause-aware executor**가 outbox의 `pending` 행을 처리(§4). unpaused일 때만 successor를 실제 enqueue.
  paused면 outbox에 남겨둔 채 skip(유실 없음). → result 저장과 cascade 실행의 결합을 끊고 crash-gap 제거.
- transition identity = `parent_task_id`(PK) → parent당 1행(dedup). enqueue race의 PausedError 경로도
  result를 이미 outbox에 갖고 있으므로(2단계에서 항상 기록) 유실 없음.

## 4. Replay state machine — lease/owner reclaim + stable transition key

v1 결함: `pending→replaying→applied`만으로는 `replaying`에서 crash한 행을 누가 언제 회수할지 미정의.
→ lease 기반 reclaim + 결정론 dedup 키 도입.

- 상태: `pending → replaying → applied`. 추가 컬럼 `attempt_id, lease_owner, lease_expires_at`(§3 스키마).
- **claim(CAS + lease)**: pause-aware executor가
  `UPDATE cascade_outbox SET state='replaying', lease_owner=?, attempt_id=?, lease_expires_at=now+LEASE_TTL
   WHERE parent_task_id=? AND (state='pending' OR (state='replaying' AND lease_expires_at < now))`
  — rowcount=1인 claimer만 진행. **stale lease(만료된 replaying)를 안전하게 reclaim**(crash한 owner 회수).
- **successor dedup = stable transition key**: successor task_id를 무작위가 아니라
  `sha1(parent_task_id, transition_kind, round)` 결정론 id로 enqueue → task_id PK 충돌로 idempotent.
  - `transition_kind` = cascade 종류(예: `implement→review`, `review→test`), `round` = 반복 회차.
  - 같은 (parent, kind, round)은 몇 번 replay돼도 successor 1개.
- **apply**: successor enqueue 성공(또는 PK충돌=이미존재) 후 CAS `UPDATE ... SET state='applied' WHERE parent_task_id=? AND lease_owner=? AND state='replaying'`.
- crash 지점:
  - (a) `pending→replaying` 후 successor enqueue 전 crash → lease 만료 후 다른 executor가 reclaim → 같은 stable id로 enqueue = at-most-once.
  - (b) successor enqueue 후 `→applied` 전 crash → reclaim 재시도 시 enqueue가 PK충돌 no-op → `applied` 마킹. 중복 successor 없음.
- **결정론 successor id + lease reclaim + CAS = crash-safe at-most-once.**

## 5. 외부 mutation(merge) — DB만으로 exactly-once 불가 → reservation + 상태재확인 + idempotent reconciliation

v1 결함: `external_op` PK로 "exactly-once"를 주장했으나 외부 GitHub 상태는 DB commit과 원자적이지 않다.
→ reservation(의도 기록) + 실제 GitHub 상태 재확인 + idempotent reconciliation으로 재정의.

- 새 테이블 `external_op(op_key TEXT PRIMARY KEY, state TEXT('reserved','done','failed'),
  pr_number INTEGER, attempt INTEGER, last_error TEXT, reserved_at REAL, done_at REAL)`.
- **reservation**: merge 전 `INSERT OR IGNORE external_op(op_key='merge:pr:<n>', state='reserved', ...)`.
  이미 `reserved`/`done`이면 아래 reconcile 경로로(중복 시작 금지, at-most-one-in-flight).
- **실행 = idempotent reconciliation(맹목 재실행 아님)**:
  1. GitHub에서 PR 실제 상태 조회(`gh pr view --json state,mergedAt,mergeStateStatus`).
  2. 이미 merged면 → `external_op.state='done'`만 기록(재merge 안 함).
  3. 아직이고 mergeable이면 → merge 시도 → 성공 시 `done`, 실패 시 `failed`+`last_error`+`attempt+1`(재시도는 backoff, 무한금지).
  4. conflict/closed 등 비가역 상태면 → `failed`로 기록하고 escalation(자동 재시도 안 함).
- crash(merge 호출 후 done 기록 전) → 재기동 시 op_key `reserved`를 보고 **1번(상태 재확인)부터** 다시 → 이미-merged 감지 → `done`. 중복 merge 없음.
- pause 게이트(이미 반영된 P0-2b)로 STOP 중 merge 미시작은 유지. reservation은 그 게이트 **뒤**에서만.

## 6. #41 post-restart ACK — DB stop epoch/incident 확인 (pause.json만으로 불충분)

DB authority가 들어가면 fleet_stop ACK 판정도 pause.json만 보면 안 된다. 재시작한 새 build가
**실제로 runtime_stop을 읽어 paused로 올라왔는지**를 canary 자격 조건으로 확인한다.

- 런타임에 read-only 상태 노출 경로 추가(택1, 구현 시 확정): `GET /status`에 `{stop_epoch, paused, incident}` 포함,
  또는 서버가 부팅 시 `runtime_stop` 값을 state.json에 미러(관측 전용).
- fleet_stop matrix의 ACK 조건(§ established): 기존 `pause.json armed(gen>=expected) + incident 일치 + 비실행(frozen/absent)`에
  **추가로** "런타임이 살아있다면(running) `/status.paused==true` AND `/status.stop_epoch>=expected` AND `/status.incident==현재`"를 요구.
  - frozen/absent는 종전대로 비실행 ACK.
  - running인데 DB paused 확인 안 되면 → UNCONFIRMED(=미봉쇄). "pause.json은 paused인데 프로세스는 무시" 케이스 차단.
- controlled restart 시퀀스: 새 build 기동 → `/status`가 DB에서 읽은 paused/epoch/incident를 반영 확인 →
  그 확인 후에만 canary 자격. 미확인이면 restart를 실패로 간주.

## 7. Migration / bootstrap

- `TaskQueue` init에서 `CREATE TABLE IF NOT EXISTS runtime_stop/cascade_outbox/external_op`.
- `runtime_stop` 없으면 default(unpaused, epoch 0) 후 §1 부팅 reconcile로 pause.json과 화해(더 높은 epoch 승자, 불일치 fail-closed).
- 기존 DB: 테이블만 추가(기존 tasks 무변경). restart-while-paused:
  pause.json paused(gen G) → 부팅 reconcile이 runtime_stop을 (paused, epoch≥G)로 수렴 → 첫 요청부터 DB 게이트.
- crash 전 `replaying` outbox 행 → 부팅 후 lease 만료 대기 없이 즉시 reclaim 가능(부팅 executor가 stale로 간주).

## Deterministic tests (필수)

- **interleaving(원자성 핵심)**: pre-check는 unpaused를 보지만, in-txn SELECT 직전 DB epoch을 paused로 advance
  → 같은 DB 도메인에서 INSERT/claim이 **rejected**. (runtime_stop 행을 직접 advance해 결정론 재현.)
- **enqueue 원자 거부**: runtime_stop paused → enqueue PausedError.
- **boot reconcile fail-closed**: db/pj 같은 epoch·다른 paused → 부팅 차단(paused 유지, 예외).
- **result+outbox 원자성**: submit_result 후 outbox에 pending 항상 존재(pause 무관). result 저장과 outbox insert가 같은 txn(중간 crash 시 둘 다 없거나 둘 다 있음).
- **replay lease reclaim**: `replaying` + 만료 lease 남긴 뒤 다른 owner가 reclaim → successor **at-most-once**(stable id dedup), `applied`로 수렴.
- **merge reconciliation**: `external_op` reserved(merge 후 crash 가정) 상태에서 재요청 → GitHub 이미-merged 감지 → 재merge 없이 `done`.
- **#41 ACK DB 확인**: running 런타임이 `/status.paused=false` → UNCONFIRMED; `/status.paused=true & epoch≥expected & incident 일치` → ACK.
- 회귀: 기존 pause 스위트 + 신규. 로컬 테스트는 증거이며 CI 아님.

## 구현 순서

1. `runtime_stop` 테이블 + `set_stop_epoch(paused,incident)`(DB먼저 commit)/`get_stop_epoch()`(queue.py) — §1
2. `enqueue/dequeue/dequeue_discuss_for_agent`를 BEGIN IMMEDIATE 안 `runtime_stop` SELECT로 게이트, pause.json in-txn 읽기 제거 — §2
3. cli `pause/resume`가 DB먼저→pause.json 미러, boot reconcile(max-epoch·불일치 fail-closed) — §1
4. `cascade_outbox` 테이블 + submit_result가 result 저장과 **같은 txn**에 outbox insert(항상) — §3
5. pause-aware outbox executor: lease claim(CAS)+stable transition key successor+apply CAS — §4
6. `external_op` reservation + GitHub 상태 재확인 + idempotent reconciliation(merge) — §5
7. `/status`에 stop_epoch/paused/incident 노출 + fleet_stop matrix ACK에 DB확인 추가 — §6
8. migration/bootstrap(CREATE IF NOT EXISTS + 부팅 reclaim) — §7
9. deterministic 테스트 전 항목 — 위 Deterministic tests
