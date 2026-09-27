"""Fast sandbox HTTP chaos check for #428 / alfred#51."""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def test_qouta_failure_contained_by_capability_registry():
    script = Path(__file__).resolve().parents[2] / "scripts/chaos/qouta_failure_containment.py"
    spec = spec_from_file_location("qouta_failure_containment", script)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    evidence = module.run()
    assert evidence["passed"]
    assert len(evidence["cases"]) == 6
    assert all(row["defense"] == "capability_registry_owner_conflict"
               for row in evidence["cases"] if row["case"].endswith("/duplicate"))
