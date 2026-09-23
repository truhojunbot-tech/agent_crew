
from agent_crew.github import post_pr_comment      # bound at collection

def test_writes_through_a_captured_alias(monkeypatch):
    import agent_crew.github as gh
    monkeypatch.setattr(gh, "check_gh_installed", lambda: True)
    monkeypatch.setattr(gh, "get_repo", lambda cwd=None: "truhojunbot-tech/agent_crew")
    post_pr_comment(999241, "this must not reach production")
