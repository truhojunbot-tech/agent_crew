import sys
import os

# Ensure this worktree's src/ is first on sys.path, overriding any system-installed
# agent_crew package. Needed when pytest is invoked from outside the repo root.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))
