# agent_crew

멀티에이전트 개발 크루 시스템. FastAPI + SQLite 기반 태스크 큐, tmux push 모델.

## Autonomous Collaboration Baseline (조직 공통 · #252)

compact/restart 후에도 유지되는 영속 지침이다. Alfred가 Chief of Staff로 조직 공통
policy를 조정하고, 이 repo는 자신의 domain authority를 유지한다.

### 역할 경계

Agent Crew는 조직의 **판단 주체가 아니라 실행/runtime 계층**이다.

| 층 | 책임 | 여기서 하지 않는 것 |
|----|------|--------------------|
| **Agent Crew** | 세션·task dispatch·review/fix lifecycle을 신뢰성 있게 제공 | 조직 정책 판단, cross-project routing, 자원 배분 결정 |
| **Alfred** | 조직 공통 policy, cross-project routing, 우선순위 판단 | — |
| **quota 계층** | provider 자원 통제·economics 측정 | — |

domain 밖 문제를 혼자 떠안지 않는다. 발견하면 근거와 함께 traceable issue를 만들고
적절한 봇/Alfred로 연결한다 (예: #247의 배포 격차를 #248로 분리, #251의 review 루프
원인을 #253으로 분리).

### 선제적 작업의 경계

- **해도 되는 것**: 안전하고 되돌릴 수 있는 내부 작업 — 조사, 측정, 테스트 추가,
  로그 분석, 문서화, 재현 스크립트.
- **먼저 물어야 하는 것**: 비가역·외부 영향 작업 — 라이브 dispatcher 재시작/배포,
  force-push, 공유 crontab 변경, 외부 서비스로의 발행. #247에서 배포는 verify 작업의
  전제조건이었지 verify 작업 자체가 아니었다.
- 지시를 기다리지 말아야 할 때: 이상징후·낭비·blocker를 **발견**했을 때. 근거와 함께
  GitHub에 남기는 것은 선제적 행동에 포함된다.

### GitHub = shared blackboard

이슈/코멘트는 성격을 먼저 밝힌다. 읽는 쪽이 무엇을 기대해야 하는지 알아야 한다.

| 성격 | 뜻 | 필수 요소 |
|------|-----|----------|
| `discovery` | 관측한 사실 | 측정값, 재현 경로, 측정 시각 |
| `proposal` | 하자는 제안 | 근거, 대안, 하지 않을 경우의 비용 |
| `blocker` | 막혔다 | 정확히 무엇이 막았는지, 누가 풀 수 있는지 |
| `result` | 끝났다 | 검증 가능한 증거 (아래) |

**추정치를 측정값처럼 쓰지 않는다.** 셀 수 없는 것은 셀 수 없다고 적는다 — #250에서
post-merge 낭비의 토큰 비용은 attribution이 남아있지 않아 "코멘트 수는 하한선"이라고
명시하고 멈췄다. 확인되지 않은 수치를 지어내는 것보다 낫다.

### quota · HOLD / VETO / STOP · escalation

- `HOLD` / `VETO` / `STOP` 신호는 **즉시** 존중한다. 진행 중 작업은 결과를 남기고 멈춘다.
- quota/자원 제한을 넘겨 작업을 밀어붙이지 않는다. provider quota 고갈은 컨텍스트 정책
  품질 실패가 아니라 외부 제약이다 (#247).
- 다음은 사용자 escalation 대상이다: 라이브 서버 재시작·배포, 공유 인프라 변경,
  되돌리기 어려운 데이터 변경, 외부로의 발행.

### runaway delegation / review cascade 방지

무한 review↔fix 루프와 task 폭발은 이 repo가 실제로 겪은 실패다. 원칙과 **현재 구현
상태**를 함께 적는다 — 미구현을 구현된 것처럼 적으면 이 문서 자체가 위험해진다.

| 메커니즘 | 무엇을 막나 | 상태 |
|----------|------------|------|
| `AGENT_CREW_REVIEW_FIX_MAX_ROUNDS` (기본 3) | 한 lineage의 무한 review↔fix | ✅ main (#244) |
| 결정론적 fix task id | 중복 result POST의 작업 분기 | ✅ main (#244) |
| build provenance gate | 배포되지 않은 코드로 측정/검증하는 것 | ✅ main (#248) |
| terminal-PR gate | merge/close된 PR에 대한 후속 작업 | ⏳ PR #251 (#250) |
| announcement atomic claim | 동시 result의 중복 escalation | ⏳ PR #251 |
| reviewed-SHA pinning | 이미 고쳐진 finding의 fix task 재생성 | ❌ 미구현 (#253) |

원칙:
1. **round cap은 lineage를 제한할 뿐, 그 작업이 여전히 유효한지는 말해주지 않는다.**
   후속 작업을 만들기 전에 대상이 아직 유효한지(PR이 열려 있는지) 확인한다.
2. **확인할 수 없으면 새 작업을 만들지 않는다.** 건너뛴 cascade는 복구 가능하지만,
   merge된 PR에 쓴 provider 호출은 복구 불가능하다. 이 비대칭이 근거다.
3. **task 결과는 언제나 보존한다.** 멈추는 것은 cascade이지 audit trail이 아니다.
4. check-then-act로 중복을 막지 않는다. 원자적 claim(PRIMARY KEY)을 쓴다.
5. 같은 finding이 반복 보고되면 재구현하지 말고 **재현을 시도하고 재현되지 않으면
   그렇게 보고한다.** 근거(commit, 시각, 재현 결과)를 함께 남긴다.

### Alfred의 policy audit / rollout 연동

- Alfred가 조직 공통 policy rollout/audit을 요청하면 수용한다. 이 절(節)이 그 착지점이다.
- domain-specific 규칙(push 모델, worker 프로토콜, dispatch 계약)은 보존한다. 충돌하면
  domain 규칙을 유지하고 충돌 사실을 issue로 남긴다.
- 적용 경로: Alfred가 issue 생성 → `crew triage --watch`가 claim → implement →
  review → PR. 즉 정책 변경도 코드 변경과 같은 증거 경로를 지난다.
- 이 문서가 갱신되면 `.claude/CLAUDE.md`(worker 프로토콜)와의 정합성을 함께 확인한다.

### 결과는 증거로 닫는다

"완료"는 다음 중 하나 이상을 동반해야 한다: 통과한 테스트와 그 수치, 측정 로그,
commit/PR 링크, 재현 스크립트, 아티팩트 파일. 확인하지 않은 것을 확인했다고 보고하지
않는다. 실패했으면 실패했다고, 건너뛰었으면 건너뛰었다고 적는다.

## Obsidian 경로

`projects/agent_crew/` 하위에 기록한다:
- `projects/agent_crew/architecture.md` — 아키텍처 문서
- `projects/agent_crew/requirements.md` — 요구사항
- `projects/agent_crew/test_plan.md` — 테스트 계획
- `projects/agent_crew/issues-YYYY-MM-DD.md` — 이슈 트래킹
- `projects/agent_crew/bug-report-YYYY-MM-DD.md` — 버그 리포트

## 주요 경로

- 소스: `~/alfred/projects/agent_crew/src/agent_crew/`
- **Worktrees**: `~/.agent_crew/worktrees/<project>/{claude,codex,gemini}/` (각 provider별 git worktree)
- **상태**: `~/.agent_crew/<project>/state.json` (port, pane_map, worktrees dict 등)
- **작업DB**: `~/.agent_crew/<project>/tasks.db` (SQLite)
- **CLI**: `~/.local/bin/crew`
- **서버**: FastAPI uvicorn, 포트는 state.json의 `port` 필드 (default 8100)

경로 커스터마이징: `crew setup <project> --base /custom/path`

## 핵심 파일

| 파일 | 역할 |
|------|------|
| `cli.py` | crew CLI 진입점 (setup/run/discuss/status/teardown) |
| `server.py` | FastAPI 태스크 서버, tmux push 담당 |
| `queue.py` | SQLite 태스크 큐 |
| `setup.py` | worktree 생성, pane 실행, 포트 관리 |
| `instructions.py` | 에이전트별 CLAUDE.md/AGENTS.md/GEMINI.md 생성 |
| `loop.py` | implement→review→test 루프 로직 |

## push 모델

서버 → `tmux paste-buffer -p` → 에이전트 pane에 `=== AGENT_CREW TASK ===` 블록 전달.  
에이전트는 `POST /tasks/<id>/result`로 결과 제출. 폴링 없음.
