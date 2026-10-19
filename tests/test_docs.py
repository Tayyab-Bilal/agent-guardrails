"""Every ```python block in README.md and docs/*.md must run. Blocks in one file share a namespace,
in order, so a later block may use names from an earlier one."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FILES = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]
FENCE = re.compile(r"^```python\n(.*?)^```", re.S | re.M)


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_doc_snippets_run(path, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # snippets may write small files
    blocks = FENCE.findall(path.read_text())
    assert blocks or path.name == "api.md", f"{path.name} has no python examples"
    ns: dict = {"__name__": "docs"}
    for i, block in enumerate(blocks, 1):
        exec(compile(block, f"{path.name} block {i}", "exec"), ns)
