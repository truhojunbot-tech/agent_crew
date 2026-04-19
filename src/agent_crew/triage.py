import re


def parse_issues(data: list[dict]) -> list[dict]:
    result = []
    for item in data:
        result.append({
            "number": item["number"],
            "title": item["title"],
            "labels": [label["name"] for label in item.get("labels", [])],
        })
    return result


def filter_processed(issues: list[dict]) -> list[dict]:
    return [i for i in issues if "agent_crew:done" not in i.get("labels", [])]


def build_prompt(issues: list[dict], merge_history: str) -> str | None:
    if not issues:
        return None
    lines = ["## Open Issues\n"]
    for issue in issues:
        labels = ", ".join(issue.get("labels", [])) or "none"
        lines.append(f"- #{issue['number']}: {issue['title']} (labels: {labels})")
    lines.append(f"\n## Recent Merge History\n{merge_history}")
    lines.append(
        "\n## Task\n"
        "Select the most important issue to work on next.\n"
        "Respond with:\n"
        "ISSUE: <number>\n"
        "DESCRIPTION: <one-line task description>"
    )
    return "\n".join(lines)


def parse_response(text: str) -> dict | None:
    issue_match = re.search(r"^ISSUE:\s*(\d+)\s*$", text, re.MULTILINE)
    desc_match = re.search(r"^DESCRIPTION:\s*(.+)\s*$", text, re.MULTILINE)
    if not issue_match or not desc_match:
        return None
    return {
        "issue": int(issue_match.group(1)),
        "description": desc_match.group(1).strip(),
    }
