# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The sink check: raw markup and script are built only in the encoder module.

`scripts/sink_check.py <root>` reads every tracked page module and served browser script
and reports each raw door (a raw primitive, a script, style or URL position fed a value, a
browser markup write or navigation) with its code. The app checkout must report none; the
seeded repositories under fixtures/sink_check hold one door per shape and must report
exactly their expected hits.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

from encode_templates import REFUSED_SHAPES

# Each check runs the sink check over a whole tree in a subprocess that may take its full wait.
pytestmark = pytest.mark.timeout(300)

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _ROOT / "scripts" / "sink_check.py"
_SEEDS = Path(__file__).resolve().parent / "fixtures" / "sink_check"
_CODES = {"PY-RAW", "PY-POSITION", "PY-COMPUTED", "PY-HTMLRESP", "PY-JSHOLE", "PY-CSSHOLE",
          "PY-URLHOLE", "JS-HTML", "JS-SCRIPT", "JS-NAV", "JS-COMPUTED", "JS-SCHEME"}


def _run(root: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(_SCRIPT), str(root)], capture_output=True,
                          text=True, timeout=300)


def _hits(out: str) -> Counter:
    return Counter(" | ".join(part.strip() for part in line.split(" | ")[:2])
                   for line in out.splitlines() if " | " in line)


def _git_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    return tmp_path


def test_sink_check_head_is_clean():
    result = _run(_ROOT)
    assert result.returncode == 0 and not result.stdout.strip(), (
        f"raw doors outside the encoder module:\n{result.stdout}{result.stderr}")


@pytest.mark.parametrize("seed", sorted(p.name for p in _SEEDS.iterdir() if p.is_dir()))
def test_sink_check_flags_seeded_mutations(seed, tmp_path):
    source = _SEEDS / seed
    repo = tmp_path / seed
    shutil.copytree(source, repo, ignore=shutil.ignore_patterns("expected.txt", "__pycache__"))
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    result = _run(repo)
    expected = Counter(line.strip() for line in (source / "expected.txt").read_text().splitlines()
                       if line.strip())
    got = _hits(result.stdout)
    assert got == expected, f"missing {expected - got}, unexpected {got - expected}\n{result.stderr}"
    assert result.returncode == min(sum(expected.values()), 255)


def test_sink_check_seeds_cover_every_code():
    seen = {line.split(" | ")[0] for p in _SEEDS.glob("*/expected.txt")
            for line in p.read_text().splitlines() if line.strip()}
    assert seen == _CODES


def test_sink_check_exempts_only_encoder_module(tmp_path):
    raw = "from fasthtml.common import NotStr, to_xml\n\ndef f(v):\n    return NotStr(to_xml(v))\n"
    repo = _git_repo(tmp_path, {
        "ui/components/encode.py": raw,
        "ui/direct.py": raw,
        "ui/star.py": "from fasthtml.common import *\n\ndef f(v):\n    return to_xml(v)\n",
        "ui/alias.py": "import fasthtml.common as fh\n\nRAW = fh.NotStr\n\ndef f(v):\n    return RAW(v)\n",
        "ui/static/celerp-dom.js": "function a(el, v) { el.innerHTML = v; }\nfunction b(v) { eval(v); }\n",
        "ui/static/page.js": "function a(el, v) { el.innerHTML = v; }\n",
    })
    hits = _hits(_run(repo).stdout)
    files = {h.split(" | ")[1].split(":")[0] for h in hits}
    assert "ui/components/encode.py" not in files
    assert {"ui/direct.py", "ui/star.py", "ui/alias.py", "ui/static/page.js"} <= files
    dom = {h.split(" | ")[0] for h in hits if "celerp-dom.js" in h}
    assert dom == {"JS-SCRIPT"}, "the browser encoder may write markup, never evaluate strings"


def test_sink_check_fails_closed(tmp_path):
    plain = tmp_path / "plain"
    (plain / "ui").mkdir(parents=True)
    (plain / "ui" / "page.py").write_text("x = 1\n")
    result = _run(plain)
    assert result.returncode == 1 and "git" in (result.stdout + result.stderr).lower()

    repo = _git_repo(tmp_path / "broken", {"ui/page.py": "def f(:\n"})
    result = _run(repo)
    assert [h.split(" | ")[0] for h in _hits(result.stdout)] == ["PARSE"]
    assert result.returncode == 1


def test_sink_check_module_root_reads_app_names(tmp_path):
    """A marketplace module is checked from its own checkout: app scripts it imports fold as
    they do in the app, and its own markup builders are module code like default_modules/."""
    repo = _git_repo(tmp_path, {
        "acme_tool/ui_routes.py": (
            "from fasthtml.common import Div, Script\n"
            "from ui.components.table import COLUMN_FILTER_JS\n\n"
            "def page(v):\n"
            "    return Div(Script(COLUMN_FILTER_JS), Script(v))\n"),
    })
    hits = _hits(_run(repo).stdout)
    assert hits == Counter({"PY-JSHOLE | acme_tool/ui_routes.py:5": 1}), hits


def test_sink_check_reports_every_refused_hole_shape(tmp_path):
    """Each template shape the encoder refuses is one PY-POSITION hit at its call; an allowed one is none."""
    lines = ["from ui.components.encode import js, raw_html", "", "def allowed(v):",
             "    return raw_html('<p title=\"{v}\">{v}</p>', v=v), js('f({0});', v)", ""]
    for i, (kind, template) in enumerate(REFUSED_SHAPES):
        call = f"raw_html({template!r}, **v)" if kind == "html" else f"js({template!r}, v)"
        lines += [f"def f{i}(v):", f"    return {call}"]
    repo = _git_repo(tmp_path, {"ui/page.py": "\n".join(lines) + "\n"})
    expected = Counter(f"PY-POSITION | ui/page.py:{7 + 2 * i}" for i in range(len(REFUSED_SHAPES)))
    assert _hits(_run(repo).stdout) == expected

