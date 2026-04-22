import uuid

from agent_crew.loop import _post_task_http
from agent_crew.protocol import TaskRequest

DEFAULT_PERSPECTIVES: list[str] = ["analyst", "critic", "advocate", "risk"]


def enqueue_panel_tasks(
    queue, agents: list[str], topic: str, context: dict, port: int = 0,
    perspectives: dict[str, str] | None = None,
) -> list[str]:
    """Enqueue one discuss task per agent.

    Each task's context carries:
      - `agent`: agent name (claude/codex/gemini) — the server uses this to
        route the push to the right pane via pane_map.
      - `perspective`: panel role (analyst/critic/...) — the agent CLI reads
        this from context to frame its opinion (see instructions.py).
    Callers should pass a pre-computed `perspectives` dict from
    `assign_perspectives(agents)` so both sides of the equation agree.
    """
    perspective_map = perspectives or assign_perspectives(agents)
    task_ids = []
    for agent in agents:
        ctx = {**context, "agent": agent, "perspective": perspective_map.get(agent, "")}
        req = TaskRequest(
            task_id=f"{agent}-{uuid.uuid4().hex[:8]}",
            task_type="discuss",
            description=f"Discuss: {topic}",
            context=ctx,
        )
        task_id = _post_task_http(port, req) if port else queue.enqueue(req)
        task_ids.append(task_id)
    return task_ids


def assign_perspectives(agents: list[str], perspectives: list[str] | None = None) -> dict[str, str]:
    pool = perspectives if perspectives is not None else DEFAULT_PERSPECTIVES
    return {agent: pool[i % len(pool)] for i, agent in enumerate(agents)}


def build_synthesis(
    results: list[dict],
    topic: str = "",
    synthesis: str = "",
    decision: str = "",
) -> str:
    lines = []
    if topic:
        lines.append(f"## Topic\n{topic}\n")
    lines.append("## Panel Opinions")
    for r in results:
        lines.append(f"\n### {r['agent']} ({r['perspective']})\n{r['summary']}")
    lines.append(f"\n## Synthesis\n{synthesis}")
    lines.append(f"\n## Decision\n{decision}")
    return "\n".join(lines)


def multi_round(queue, agents: list[str], topic: str, rounds: int = 1) -> str:
    synthesis = ""
    for round_num in range(1, rounds + 1):
        context: dict = {"round": round_num}
        if round_num > 1 and synthesis:
            context["prior_synthesis"] = synthesis

        enqueue_panel_tasks(queue, agents, topic, context)

        results = []
        perspectives = assign_perspectives(agents)
        for agent in agents:
            results.append({
                "agent": agent,
                "perspective": perspectives[agent],
                "summary": f"Round {round_num} input from {agent}.",
            })

        synthesis = build_synthesis(results, topic=topic)

    return synthesis


def then_run(synthesis: str) -> str:
    return synthesis
