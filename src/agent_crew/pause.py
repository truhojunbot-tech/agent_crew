"""pause.py — Agent Crew 런타임 pause/STOP 계약 (agent_crew#311, 부모 incident alfred#39).

incident gap: feeder-level STOP은 Alfred의 신규 dispatch만 막았고, Agent Crew 서버는
이미 큐에 있던 task를 계속 드레인했다. 즉 STOP이 실행 런타임까지 강하게 전파되지 않았다.

이 모듈은 **generic runtime pause 계약**이다. 외부 system manager(Alfred 등)가 안정적
인터페이스로 구동한다. **PORTABLE_CORE**: Alfred/사설 fleet 상태를 import하지 않는다.

pause 범위:
 - project pause: `<state_dir>/pause.json`
 - runtime/global pause: `AGENT_CREW_PAUSE_FILE` 또는 `~/.agent_crew/GLOBAL_PAUSE.json`
둘 중 하나라도 활성이면 paused.

pause 레코드: {paused, scope, reason, source, incident, generation, activated_at}
 - persisted (서버 재시작 후에도 유지)
 - resume은 generation-aware: stale(≤현재) resume은 최신 STOP을 덮지 못함.
"""
from __future__ import annotations
import os, json, datetime
from typing import Optional, Dict, Any

GLOBAL_PAUSE_FILE = os.environ.get(
    "AGENT_CREW_PAUSE_FILE",
    os.path.join(os.path.expanduser("~"), ".agent_crew", "GLOBAL_PAUSE.json"),
)


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class _LoadError(dict):
    """파일이 존재하나 읽기/파싱 실패 — fail-closed 신호(=paused로 취급)."""
    pass


def _load(path: str) -> Optional[Dict[str, Any]]:
    """부재(=None, 정상 미pause) vs 손상(_LoadError, fail-closed=paused)을 구분한다.
    안전 STOP에서는 상태를 확신할 수 없으면 paused여야 하므로, 손상은 절대 None으로 접지 않는다."""
    if not path:
        return None
    if not os.path.isfile(path):
        return None            # 파일 없음 = 명시적으로 pause 아님(정상)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return _LoadError({"error": "not-a-dict", "path": path})
        return data
    except Exception as e:      # 존재하나 손상 → fail-closed
        return _LoadError({"error": repr(e)[:120], "path": path})


def _save(path: str, rec: Dict[str, Any]) -> None:
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _project_pause_path(state_dir: str) -> str:
    return os.path.join(state_dir, "pause.json")


def _active(rec: Optional[Dict[str, Any]]) -> bool:
    # fail-closed: 손상된 상태파일(_LoadError)은 무조건 활성(paused)로 취급.
    if isinstance(rec, _LoadError):
        return True
    return bool(rec and rec.get("paused"))


def pause_state(state_dir: str = "") -> Dict[str, Any]:
    """현재 pause 판정 + provenance. 어떤 scope가 왜 막는지 노출(telemetry)."""
    proj = _load(_project_pause_path(state_dir)) if state_dir else None
    glob = _load(GLOBAL_PAUSE_FILE)
    active_scopes = []
    if _active(glob) and glob:
        active_scopes.append({"scope": "global", **glob})
    if _active(proj) and proj:
        active_scopes.append({"scope": "project", **proj})
    return {
        "paused": bool(active_scopes),
        "active_scopes": active_scopes,
        "global": glob,
        "project": proj,
    }


def is_paused(state_dir: str = "") -> bool:
    return pause_state(state_dir)["paused"]


def blocked_reason(state_dir: str = "") -> str:
    st = pause_state(state_dir)
    if not st["paused"]:
        return ""
    parts = []
    for s in st["active_scopes"]:
        if s.get("error"):
            parts.append(f"{s['scope']}(FAIL-CLOSED: state 손상 {s['error']})")
        else:
            parts.append(f"{s['scope']}(incident={s.get('incident')},gen={s.get('generation')},reason={s.get('reason')})")
    return "paused by " + ", ".join(parts)


def set_pause(state_dir: str, paused: bool, *, scope: str = "project",
              reason: str = "", source: str = "", incident: Optional[str] = None,
              generation: Optional[int] = None) -> Dict[str, Any]:
    """pause 설정. generation 미지정 시 기존+1(monotonic). scope=global이면 전역 파일."""
    path = GLOBAL_PAUSE_FILE if scope == "global" else _project_pause_path(state_dir)
    cur = _load(path) or {}
    gen = generation if generation is not None else int(cur.get("generation", 0)) + 1
    rec = {
        "paused": bool(paused), "scope": scope, "reason": reason,
        "source": source, "incident": incident, "generation": gen,
        "activated_at": _now() if paused else None,
        "updated_at": _now(),
    }
    _save(path, rec)
    return rec


def resume(state_dir: str, *, scope: str = "project", generation: int,
           source: str = "") -> Dict[str, Any]:
    """generation-aware resume. stale(요청 gen ≤ 현재 gen)이면 거부 — 최신 STOP을 못 덮는다.
    resume은 task를 복제하거나 lineage를 리셋하지 않는다(상태 파일만 변경)."""
    path = GLOBAL_PAUSE_FILE if scope == "global" else _project_pause_path(state_dir)
    cur = _load(path)
    # fail-closed(#39): 상태파일이 손상(_LoadError)이면 pause 여부를 확신할 수 없으므로
    # resume을 거부한다. 모르는 상태에서 resume이 열리면 STOP이 뚫린다.
    if isinstance(cur, _LoadError):
        return {"resumed": False, "reason": f"FAIL-CLOSED: pause state 손상 {cur.get('error')} — resume 거부",
                "still_paused": True}
    cur = cur or {}
    cur_gen = int(cur.get("generation", 0))
    if not cur.get("paused"):
        return {"resumed": True, "reason": "not paused", "generation": cur_gen}
    if int(generation) <= cur_gen:
        return {"resumed": False, "reason": f"stale resume gen {generation} <= current {cur_gen} — 거부",
                "generation": cur_gen, "still_paused": True}
    rec = {"paused": False, "scope": scope, "reason": "resumed", "source": source,
           "incident": cur.get("incident"), "generation": int(generation),
           "activated_at": None, "updated_at": _now()}
    _save(path, rec)
    return {"resumed": True, "generation": int(generation)}
