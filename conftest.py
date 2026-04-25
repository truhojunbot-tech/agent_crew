import os
import sys

_repo_src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")

# Remove competing agent_crew installs (e.g. pip editable install at main repo).
# Must run before any test imports so the wrong package is never loaded.
for _p in [
    p for p in sys.path
    if p and p != _repo_src and os.path.isfile(os.path.join(p, "agent_crew", "__init__.py"))
]:
    sys.path.remove(_p)

if _repo_src in sys.path:
    sys.path.remove(_repo_src)
sys.path.insert(0, _repo_src)

# Purge any already-cached agent_crew modules so re-imports pick up _repo_src.
for _k in [k for k in sys.modules if k == "agent_crew" or k.startswith("agent_crew.")]:
    del sys.modules[_k]
