# ContextPack Golden Set v1 — 20건 (2026-09-28)

NF-02 방식. 데이터 출처: agent_crew /tasks API (8103 alfred, 8105 agent_crew).
각 케이스는 "리셋 후 잊으면 안 되는 문맥"이 실제로 없어서 문제가 생긴 패턴 또는
없었다면 생겼을 패턴을 기반으로 한다.

## 스키마

```yaml
id: gc-NNN
type: prior_finding | ac | owner_decision | linked_review
source_task: task_id
issue: GitHub issue number (있으면)
query: fresh 세션 에이전트가 할 질문
expected_pack_items:
  - artifact_type: ...
    uri: ...
    why: ...
expected_missing_signals: []  # is_sufficient가 경고해야 할 신호
actual_outcome: 실제 이력에서 일어난 일
```

---

## 유형 1: prior_finding — 이전 라운드 지적 (7건)

### gc-001
```yaml
id: gc-001
type: prior_finding
source_task: review-fix-453-r2-one-command-r0
issue: 453
query: "이전 리뷰에서 어떤 문제를 지적했나?"
expected_pack_items:
  - artifact_type: pr_review
    uri: "https://github.com/truhojunbot-tech/agent_crew/pull/454"
    why: "fix round r2가 r1 review의 scope invariant 지적을 다시 위반함"
expected_missing_signals:
  - "fix round requires linked_review — prior findings unknown; reviewer may re-raise same issues"
actual_outcome: |
  r2 implementer가 r1 review에서 '스크립트는 broker restart를 하지 않는다'는
  invariant를 명시했음에도 live restart 코드를 추가. 리뷰어가 SCOPE_VIOLATION으로
  다시 REQUEST_CHANGES. 팩에 linked_review가 없었기 때문.
```

### gc-002
```yaml
id: gc-002
type: prior_finding
source_task: review-fix-review-sev0-tokenomics-canary-one-r2-atomic-suppression-r0-r1-r1
issue: 51
query: "이전 라운드(r1) 리뷰의 5가지 지적 중 어떤 것이 아직 열려 있나?"
expected_pack_items:
  - artifact_type: pr_review
    uri: "sev0/cea-lineage@00163b2"
    why: "r1 리뷰의 5개 finding 중 1개가 fd4f4cb에서도 미해결"
expected_missing_signals:
  - "fix round requires linked_review — prior findings unknown; reviewer may re-raise same issues"
actual_outcome: |
  리뷰어: "Four of my five previous findings are genuinely fixed... [1개 미해결]"
  linked_review가 없으면 어떤 finding이 열려 있는지 알 수 없음.
```

### gc-003
```yaml
id: gc-003
type: prior_finding
source_task: review-fix-review-adr-row14-pr85-r1-manual-b-r1
issue: null
query: "PR #85 r1 리뷰에서 blocking finding이 무엇이었나?"
expected_pack_items:
  - artifact_type: pr_review
    uri: "https://github.com/truhojunbot-tech/alfred/pull/85"
    why: "r1 리뷰의 'reports/ 파일' blocking finding이 r2에서 재제기됨"
expected_missing_signals:
  - "fix round requires linked_review — prior findings unknown; reviewer may re-raise same issues"
actual_outcome: |
  r2에서 "Blocking: reports/..." 동일 지적이 재등장.
  리뷰어가 이전 finding을 참조할 수 있었다면 즉시 확인 가능했음.
```

### gc-004
```yaml
id: gc-004
type: prior_finding
source_task: review-362-dispatch-guards
issue: 362
query: "issue #362 dispatch-guards 리뷰에서 unresolved P1은 무엇인가?"
expected_pack_items:
  - artifact_type: pr_review
    uri: "https://github.com/truhojunbot-tech/agent_crew/pull/xxx"
    why: "unguarded watchdog tmux sends P1이 fix round에서 재확인 필요"
expected_missing_signals:
  - "fix round requires linked_review — prior findings unknown; reviewer may re-raise same issues"
actual_outcome: |
  REQUEST_CHANGES: "two P1s: unguarded watchdog tmux sends, and ownership frozen at startup
  that livelocks after a pane_map reload". fix round에 이 정보가 없으면 pane_map livelock
  수정이 누락됨.
```

### gc-005
```yaml
id: gc-005
type: prior_finding
source_task: review-impl-adr-row12-supersedes-producer-r0
issue: null
query: "PR #74 리뷰의 finding에서 '증명이 필요한 관계'가 무엇인가?"
expected_pack_items:
  - artifact_type: pr_review
    uri: "https://github.com/truhojunbot-tech/alfred/pull/74"
    why: "NEW-supersedes-OLD operator-assert finding이 fix round에서 주소되어야 함"
expected_missing_signals:
  - "fix round requires linked_review — prior findings unknown"
actual_outcome: |
  REQUEST_CHANGES: "NEW-supersedes-OLD relation is operator-asserted, not verified".
  fix round 없이는 이 검증 요건이 전달되지 않음.
```

### gc-006
```yaml
id: gc-006
type: prior_finding
source_task: review-impl-policy-snapshot-ed25519-r0
issue: null
query: "PR #78 Ed25519 리뷰의 blocking finding은 무엇인가?"
expected_pack_items:
  - artifact_type: pr_review
    uri: "https://github.com/truhojunbot-tech/alfred/pull/78"
    why: "verify() HMAC-only fallback 취약점 — fix round에서 반드시 닫아야 함"
expected_missing_signals:
  - "fix round requires linked_review — prior findings unknown"
actual_outcome: |
  REQUEST_CHANGES: "verify() skips Ed25519 when block absent → HMAC-only-resigned snapshot
  returns valid". fix round에 이 패턴 없으면 동일 취약점 재현 가능.
```

### gc-007
```yaml
id: gc-007
type: prior_finding
source_task: review-sev0-p0-2-g12-cancel-invariants-r1-r0
issue: 51
query: "r1 리뷰에서 P1 cancellation invariant 지적이 무엇이었나?"
expected_pack_items:
  - artifact_type: pr_review
    uri: "sev0/cea-post-sprint@720ac76"
    why: "cancellation leaves live lease with no end event — r2 fix target"
expected_missing_signals:
  - "fix round requires linked_review — prior findings unknown"
actual_outcome: |
  r1 REQUEST_CHANGES: "cancellation leaves a live lease/no end event".
  r2 implementer가 이 finding을 모르면 lease cleanup을 누락함.
```

---

## 유형 2: ac — Acceptance Criteria (5건)

### gc-008
```yaml
id: gc-008
type: ac
source_task: review-362-teardown
issue: 362
query: "issue #362의 acceptance criteria는 무엇인가?"
expected_pack_items:
  - artifact_type: acceptance_criteria
    uri: "https://github.com/truhojunbot-tech/agent_crew/issues/362"
    why: "teardown safety conditions이 AC에 정의됨"
expected_missing_signals:
  - "AC artifact missing — acceptance bar unknown"
actual_outcome: |
  AC가 팩에 없어 리뷰어가 safety 요건을 issue에서 직접 재확인해야 했음.
  "detached HEAD" 케이스가 AC에 명시됐는지 리뷰어가 모름.
```

### gc-009
```yaml
id: gc-009
type: ac
source_task: review-fix-453-r2-one-command-r0
issue: 453
query: "issue #453의 '스크립트 범위' AC는 무엇인가?"
expected_pack_items:
  - artifact_type: acceptance_criteria
    uri: "https://github.com/truhojunbot-tech/agent_crew/issues/453"
    why: "'no broker process restart' 불변식이 AC에 명시되어야 함"
expected_missing_signals:
  - "AC artifact missing — acceptance bar unknown"
actual_outcome: |
  AC 없이 implementer가 r2에서 AC 범위를 초과. 'dry-run precondition checking'만
  AC에 있었다면 broker restart 코드 추가가 scope violation임을 알 수 있었음.
```

### gc-010
```yaml
id: gc-010
type: ac
source_task: review-impl-tok31-context-pack-shadow-r2-r0
issue: 44
query: "issue #44 context pack shadow의 AC: shadow는 프롬프트에 영향 없어야 하나?"
expected_pack_items:
  - artifact_type: acceptance_criteria
    uri: "https://github.com/truhojunbot-tech/agent_crew/issues/44"
    why: "AC: 'message/task row untouched' — 리뷰어가 이를 기준으로 검증"
expected_missing_signals: []
actual_outcome: |
  이 케이스는 AC가 팩에 있었어야 하는데 shadow 데이터에서 has_AC=False.
  리뷰어 summary에서 'message/task row untouched' 확인 — AC 출처 불명.
```

### gc-011
```yaml
id: gc-011
type: ac
source_task: review-impl-adr-row24-equality-r0
issue: null
query: "row 2.4 equality gate의 AC: 기존 gate 재작성이 허용되나?"
expected_pack_items:
  - artifact_type: spec
    uri: "docs/adr/..."
    why: "gate 재작성 vs 유지가 AC에 명시되어야 함"
expected_missing_signals:
  - "AC artifact missing — acceptance bar unknown"
actual_outcome: |
  REQUEST_CHANGES: "the PR rewrites row 2.4's gate from master's version, violating the
  spec's 'gate unchanged unless explicitly approved' invariant". AC가 있었다면 이 변경이
  허용 범위 외임을 implementer가 알았을 것.
```

### gc-012
```yaml
id: gc-012
type: ac
source_task: review-sev0-cea-lineage-s1
issue: 51
query: "sev0 s1 step의 AC: 어떤 P6 항목이 필수인가?"
expected_pack_items:
  - artifact_type: acceptance_criteria
    uri: "https://github.com/truhojunbot-tech/agent_crew/issues/51"
    why: "P6 runtime adapter AC가 step 판정 기준"
expected_missing_signals:
  - "AC artifact missing — acceptance bar unknown"
actual_outcome: |
  리셋 후 fresh implementer가 P6 범위를 재파악해야 함. AC 없이 진행하면
  P6 일부를 '완료'로 잘못 판정.
```

---

## 유형 3: owner_decision — 오너 결정 (4건)

### gc-013
```yaml
id: gc-013
type: owner_decision
source_task: review-fix-453-r2-one-command-r0
issue: 453
query: "오너가 issue #453 스크립트에 'no broker restart' 제약을 명시했나?"
expected_pack_items:
  - artifact_type: spec
    uri: "docs/adr/... or issue #453"
    why: "오너 결정: dry-run only, broker lifecycle 금지"
expected_missing_signals:
  - "AC artifact missing — acceptance bar unknown"
actual_outcome: |
  r2 implementer가 오너 제약을 모르고 restart 코드 추가. 오너 결정이 팩에
  있었다면 구현 전에 제약을 인지.
```

### gc-014
```yaml
id: gc-014
type: owner_decision
source_task: review-sev0-p0-2-g12-cancel-invariants-r2-claude-r0
issue: 51
query: "오너가 self-review를 명시적으로 금지했나?"
expected_pack_items:
  - artifact_type: spec
    uri: "procedure://independence-rule"
    why: "INDEPENDENCE BLOCKER: reviewer != implementer — 오너 결정"
expected_missing_signals:
  - "AC artifact missing"
actual_outcome: |
  "⛔INDEPENDENCE BLOCKER — This review was dispatched to claude, but claude IMPLEMENTED".
  procedure 팩이 있었다면 dispatch 전에 이 케이스를 막을 수 있었음.
```

### gc-015
```yaml
id: gc-015
type: owner_decision
source_task: review-adr-row14-recall-gate-pr85
issue: null
query: "row 1.4 recall gate의 오너 결정: held-out validation이 필수인가?"
expected_pack_items:
  - artifact_type: adr
    uri: "docs/adr/row-1.4..."
    why: "오너 결정: independent held-out validation 없으면 gate=false"
expected_missing_signals:
  - "AC artifact missing"
actual_outcome: |
  REQUEST_CHANGES: "separate held-out validation is unmet". ADR에 이 결정이 있었지만
  팩에 포함 안 됨. 리뷰어가 ADR을 직접 조회해야 했음.
```

### gc-016
```yaml
id: gc-016
type: owner_decision
source_task: review-impl-adr-matrix-rows1x-24mech-r0
issue: null
query: "row 1.2 MEASURED-FAIL 판정: 오너가 fail 유지를 결정했나?"
expected_pack_items:
  - artifact_type: adr
    uri: "docs/adr/matrix..."
    why: "오너 결정: 1.2 fail 상태 유지, fix 없이 MEASURED-FAIL 기록"
expected_missing_signals: []
actual_outcome: |
  이 케이스는 팩이 ADR을 포함하여 APPROVE됨. 오너 결정이 팩에 있는
  positive case — golden set의 '주입 효과' 측정 기준.
```

---

## 유형 4: linked_review — Linked PR 리뷰 내용 (4건)

### gc-017
```yaml
id: gc-017
type: linked_review
source_task: review-fix-453-r2-one-command-r0
issue: 453
query: "이전 리뷰(review-impl-453-dryrun-preconditions-r0)의 모든 blocking finding은?"
expected_pack_items:
  - artifact_type: pr_review
    uri: "agent_crew#453 review-impl-453-r0"
    why: "r2 fix 대상 finding 목록"
expected_missing_signals:
  - "fix round requires linked_review"
actual_outcome: |
  linked_review 없이 r2가 진행. 결과: scope 초과 + 새 P1 도입.
  fix-r3에서야 "comprehensively resolves both prior findings" — 2 라운드 낭비.
```

### gc-018
```yaml
id: gc-018
type: linked_review
source_task: review-fix-review-adr-row14-pr85-r1-manual-b-r1
issue: null
query: "review-adr-row14-recall-gate-pr85의 모든 blocking finding은?"
expected_pack_items:
  - artifact_type: pr_review
    uri: "alfred PR #85 review-adr-row14-recall-gate-pr85"
    why: "r1 blocking finding: 'reports/' 파일 처리"
expected_missing_signals:
  - "fix round requires linked_review"
actual_outcome: |
  r1 리뷰 "reports/" finding이 r2에서 재제기. 만약 r1 review artifact가
  팩에 있었다면 fix에서 이미 닫혔을 것.
```

### gc-019
```yaml
id: gc-019
type: linked_review
source_task: review-fix-review-sev0-tokenomics-canary-one-r2-atomic-suppression-r0-r1-r1
issue: 51
query: "r2-atomic-suppression r0 리뷰의 미해결 finding은?"
expected_pack_items:
  - artifact_type: pr_review
    uri: "sev0/cea-lineage r0 review"
    why: "5개 finding 중 r1에서 1개 미해결 → r2 fix 대상"
expected_missing_signals:
  - "fix round requires linked_review"
actual_outcome: |
  linked_review가 있었다면 5→4→1개 finding 추적이 자동화됨.
  없이는 리뷰어가 "four of my five previous findings" 수작업 재확인.
```

### gc-020
```yaml
id: gc-020
type: linked_review
source_task: review-impl-policy-snapshot-ed25519-contract-r0
issue: null
query: "PR #78 r0 리뷰의 Ed25519 blocking finding 목록은?"
expected_pack_items:
  - artifact_type: pr_review
    uri: "alfred PR #78 d3ee0da"
    why: "verify() Ed25519 skip vulnerability — contract fix에서 닫혀야 함"
expected_missing_signals:
  - "fix round requires linked_review"
actual_outcome: |
  PR #78 contract review: "APPROVE. The Ed25519 downgrade is fixed (stripped block,
  HMAC-only-resign attack closed)". linked_review 포함 시 fix가 1라운드에 완결됨.
```

---

## 집계

| 유형 | 건수 | 재제기 발생 | expected missing_signal |
|------|------|-------------|--------------------------|
| prior_finding | 7 | 7/7 | fix round requires linked_review |
| ac | 5 | 3/5 | AC artifact missing |
| owner_decision | 4 | 2/4 | AC / spec artifact missing |
| linked_review | 4 | 3/4 | fix round requires linked_review |
| **합계** | **20** | **15/20** | |

## 검증 방법 (NF-02)

1. 각 케이스에 대해 task API에서 실제 팩 telemetry 조회 (has_AC, has_linked_review)
2. `is_sufficient()` 적용 → expected_missing_signals와 대조
3. actual_outcome의 재제기 여부가 missing_signal과 상관됨을 확인

precision 목표: 15/20 = 75%가 missing_signal → actual 재제기로 연결.
