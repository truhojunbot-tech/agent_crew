# ContextPack.is_sufficient() 규격 v1 (2026-09-28)

## 목적

세션 리셋(300k hard cap 발동) 직후 fresh 세션에 context pack을 **주입할지** 판정한다.
리셋 여부 결정이 아니다 — 300k cap은 비용 hard reset으로 독립 동작한다.

## 반환 타입

```python
@dataclass
class InjectGate:
    ok: bool                    # True → 주입, False → missing_signals 기록 후 경고
    missing_signals: list[str]  # 각 미충족 요건을 한 줄씩
```

## 판정 규칙 (우선순위 순)

| # | 조건 | missing_signal 문자열 | 비고 |
|---|------|-----------------------|------|
| 1 | issue artifact 없음 | `"issue artifact missing — agent cannot identify what it is fixing"` | 항상 필수 |
| 2 | AC artifact 없음 AND has_ac_checked=False | `"AC artifact missing — acceptance bar unknown; annotate no_ac=true if issue has none"` | AC 없는 이슈는 `no_ac=True`로 명시해야 통과 |
| 3 | task_type='fix' AND linked_review artifact 없음 | `"fix round requires linked_review — prior findings unknown; reviewer may re-raise"` | fix round 식별: task_type 또는 task_id에 'fix' 포함 |
| 4 | retry_of 있음 AND 해당 task_id의 episodic artifact 없음 | `"retry of {retry_of} but no episodic record — prior failure reason unknown"` | retry_of 필드 기반 |

**모든 조건을 통과해야 `ok=True`.** 하나라도 미충족이면 `ok=False`, missing_signals에 추가.

## 구현 스펙

```python
from dataclasses import dataclass, field

@dataclass
class InjectGate:
    ok: bool
    missing_signals: list = field(default_factory=list)

def is_sufficient(pack: ContextPack, *, task_type: str = "", retry_of: str = "") -> InjectGate:
    """Gate: 리셋 후 pack 주입 여부 판정.

    ok=False여도 팩은 주입된다 — missing_signals를 경고 블록으로 함께 주입.
    주입을 막는 게 아니라 에이전트에게 무엇이 없는지 알려주는 것이 목적.
    """
    signals = []
    artifact_types = {a.artifact_type for a in pack.items}

    # Rule 1: issue mandatory
    if TYPE_ISSUE not in artifact_types:
        signals.append("issue artifact missing — agent cannot identify what it is fixing")

    # Rule 2: AC mandatory (unless no_ac explicitly flagged)
    has_no_ac_flag = any(
        getattr(a, 'no_ac', False) or 'no_ac' in (a.provenance or '')
        for a in pack.items if a.artifact_type == TYPE_ISSUE
    )
    if TYPE_AC not in artifact_types and not has_no_ac_flag:
        signals.append(
            "AC artifact missing — acceptance bar unknown; "
            "annotate no_ac=true in IssueProvider if issue has none"
        )

    # Rule 3: fix round → linked_review required
    is_fix = 'fix' in (task_type or '').lower() or pack.task_id.startswith('fix-')
    if is_fix and TYPE_REVIEW not in artifact_types:
        signals.append(
            "fix round requires linked_review — prior findings unknown; "
            "reviewer may re-raise same issues"
        )

    # Rule 4: retry → episodic of prior attempt required
    if retry_of:
        has_prior_episodic = any(
            a.artifact_type == TYPE_EPISODE and retry_of in a.artifact_id
            for a in pack.items
        )
        if not has_prior_episodic:
            signals.append(
                f"retry of {retry_of} but no episodic record — "
                f"prior failure reason unknown"
            )

    return InjectGate(ok=not signals, missing_signals=signals)
```

## 프롬프트 블록 확장

pack.to_prompt_block()은 InjectGate를 받아 경고를 삽입한다:

```
=== CONTEXT PACK cp1-abcd1234 (mode=lexical, 5 items, ~3200 tok) ===
⚠️ INJECT WARNING (ok=False):
  - fix round requires linked_review — prior findings unknown; reviewer may re-raise same issues
--- [issue] ...
```

`ok=False`여도 팩은 주입된다. 에이전트가 경고를 보고 링크된 리뷰를 직접 조회하도록 유도한다.

## 측정 지표 연결

| 지표 | 측정 방법 |
|------|-----------|
| 재제기율 (±10% 기준) | missing_signal='fix round...'로 주입된 리뷰 판정 vs warm 리뷰 판정 |
| 수정 라운드 (+1 이내) | task_id별 fix round 횟수 |
| 리뷰당 토큰 (50%↓) | warm_context_tokens vs pack_tokens (shadow 데이터) |

## no_ac 처리 가이드

IssueProvider.retrieve()에서 AC 섹션을 찾지 못하면:
- 기존: `logger.info("issue has no acceptance-criteria section")` — 통과
- 변경: Artifact.provenance에 `no_ac=true` 마킹 → Rule 2 통과 허용

이슈 body 자체가 없는 경우(lookup_failed)는 Rule 1·2 모두 실패 → pack은 degraded와 함께 주입.

## 담당
- **인터페이스 정의**: Context Forge 봇 (이 문서)
- **구현**: agent-crew 봇 (agent_crew/context_pack.py에 `is_sufficient()` 추가)
- **PR 제목**: `[agent: codex] feat: add ContextPack.is_sufficient() inject gate`
- **env flag**: `AGENT_CREW_CONTEXT_PACK_INJECT_GATE=1` (기본 off)
