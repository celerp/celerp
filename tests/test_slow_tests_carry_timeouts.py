"""A test whose measured CI time is past two thirds of the suite's 30s per-test cap carries its
own timeout mark. The runners swing by a third from run to run, so such a test would otherwise
be killed mid-teardown on a slow runner and leave its worker's database to the next test."""
import ast
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SUITE_CAP_S = int(re.search(r"--timeout=(\d+)", (ROOT / ".github/workflows/ci.yml").read_text()).group(1))


def _has_timeout(nodes) -> bool:
    return any(isinstance(n, ast.Attribute) and n.attr == "timeout" for node in nodes for n in ast.walk(node))


def _marked(path: Path, names: list[str]) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    if _has_timeout(s.value for s in tree.body if isinstance(s, ast.Assign)
                    and any(getattr(t, "id", None) == "pytestmark" for t in s.targets)):
        return True
    scope = tree.body
    for name in names:
        node = next((n for n in scope if getattr(n, "name", None) == name), None)
        if node is None:   # a recorded id whose test has since been renamed or removed
            return True
        if _has_timeout(node.decorator_list):
            return True
        scope = getattr(node, "body", [])
    return False


def test_every_test_slower_than_two_thirds_of_the_cap_carries_its_own_timeout():
    durations = json.loads((ROOT / "tests/.test_durations").read_text())
    unmarked = sorted({
        test_id.split("[")[0] for test_id, seconds in durations.items()
        if seconds > SUITE_CAP_S * 2 / 3 and (ROOT / test_id.split("::")[0]).is_file()
        and not _marked(ROOT / test_id.split("::")[0], test_id.split("[")[0].split("::")[1:])
    })
    assert unmarked == []
