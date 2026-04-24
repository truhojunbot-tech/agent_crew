# agent_crew

멀티에이전트 개발 크루 시스템. FastAPI + SQLite 기반 태스크 큐, tmux push 모델.

## Obsidian 경로

`projects/agent_crew/` 하위에 기록한다:
- `projects/agent_crew/architecture.md` — 아키텍처 문서
- `projects/agent_crew/requirements.md` — 요구사항
- `projects/agent_crew/test_plan.md` — 테스트 계획
- `projects/agent_crew/issues-YYYY-MM-DD.md` — 이슈 트래킹
- `projects/agent_crew/bug-report-YYYY-MM-DD.md` — 버그 리포트

## 주요 경로

- 소스: `~/alfred/projects/agent_crew/src/agent_crew/`
- Worktrees: `~/.agent_crew/<project>/{claude,codex,gemini}/` (각 provider별 git worktree)
  - 사용자 정의 가능: `crew setup <project> --base /custom/path`
- 상태: `~/.agent_crew/<project>/state.json` (port, pane_map, worktrees 등)
- 작업DB: `~/.agent_crew/<project>/tasks.db` (SQLite)
- CLI: `~/.local/bin/crew`
- 서버: FastAPI uvicorn, 포트는 state.json의 `port` 필드 (default 8100)

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
