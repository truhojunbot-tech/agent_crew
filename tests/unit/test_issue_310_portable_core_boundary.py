"""Guard the portable core while existing fleet defaults await later #310 slices."""

import ast
import sys
from pathlib import Path


SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src" / "agent_crew"

# Public optional integrations already imported by the package. Any new
# non-stdlib import needs an explicit boundary review before it is allowed.
PUBLIC_IMPORT_ROOTS = {
    "agent_crew", "click", "cryptography", "fastapi", "httpx",
    "jsonschema", "mcp", "pydantic",
    # Optional memory-hybrid extra: lazy imports; absent dependencies use lexical fallback.
    "numpy", "onnxruntime", "tokenizers",
}

# Existing literals only. Keys are source file:line and the private marker;
# moving or adding one requires recording why it belongs in a later slice.
PRIVATE_LITERAL_ALLOWLIST = {
    ("cea/input_providers/admission_inputs.py", 28, "/home/truhojun"):
        "Optional CEA subprocess default; an environment path can replace it.",
    ("cea/input_providers/budget.py", 36, "/home/truhojun"):
        "Optional CEA quota cache default; an environment path can replace it.",
    ("cea/input_providers/snapshot.py", 46, "/home/truhojun"):
        "Optional CEA snapshot default; an environment path can replace it.",
    ("cea/wiring.py", 69, "/home/truhojun"):
        "Optional CEA capability registry default; environment configurable.",
    ("cea/wiring.py", 70, "/home/truhojun"):
        "Optional CEA policy snapshot default; environment configurable.",
    ("cea/wiring.py", 71, "/home/truhojun"):
        "Optional CEA memory command default; environment configurable.",
}


def _modules():
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        yield path.relative_to(SOURCE_ROOT).as_posix(), ast.parse(path.read_text())


def test_portable_core_has_no_private_package_imports():
    allowed = sys.stdlib_module_names | PUBLIC_IMPORT_ROOTS
    offenders = []
    for filename, tree in _modules():
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module] if node.module else []
            else:
                continue
            for name in names:
                root = name.split(".", 1)[0]
                if root not in allowed:
                    offenders.append(f"{filename}:{node.lineno}: {name}")
    assert offenders == [], "private or undeclared package imports: " + ", ".join(offenders)


def test_portable_core_has_no_new_private_home_literals():
    found = set()
    for filename, tree in _modules():
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                for marker in ("/home/truhojun", "~/alfred"):
                    if marker in node.value:
                        found.add((filename, node.lineno, marker))
    assert found == set(PRIVATE_LITERAL_ALLOWLIST), (
        f"new private literals: {sorted(found - PRIVATE_LITERAL_ALLOWLIST.keys())}; "
        f"stale allowlist entries: {sorted(PRIVATE_LITERAL_ALLOWLIST.keys() - found)}"
    )
