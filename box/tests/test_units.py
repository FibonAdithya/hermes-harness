"""systemd expands %-specifiers in Exec lines before the shell sees them.

`printf %s` in box/units/harness-pull.service became `printf /bin/bash` (%s is the
user's shell), so the deploy stamp held "/bin/bash" and never matched. A literal
percent sign must be written %%.
"""
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
UNITS = sorted((REPO / "box" / "units").glob("*.service")) + sorted((REPO / "droplet" / "units").glob("*.service"))
# Specifiers these units mean to use: %h (home directory) and %% (a literal %).
ALLOWED = {"h", "%"}


def _exec_lines(path):
    for n, line in enumerate(path.read_text().splitlines(), 1):
        if re.match(r"\s*Exec\w*=", line):
            yield n, line


def test_units_found():
    assert any(p.name == "harness-pull.service" and p.parent.parent.name == "box" for p in UNITS)


@pytest.mark.parametrize("unit", UNITS, ids=lambda p: f"{p.parent.parent.name}/{p.name}")
def test_exec_lines_use_only_intended_specifiers(unit):
    bad = []
    for n, line in _exec_lines(unit):
        for m in re.finditer(r"%(.)", line):
            if m.group(1) not in ALLOWED:
                bad.append(f"line {n}: %{m.group(1)}")
    assert not bad, f"{unit.name}: systemd would expand {bad}; write a literal % as %%"
