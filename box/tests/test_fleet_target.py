"""hermes-harness as a task target: fleet.toml and the ownership map that file_task reads.

fleet is not installed where this suite runs, so the table reader below is a
copy of fleet's (fleet.policy._rows and parse_ownership, at fleet 5997ac2) and
must fail on the same text fleet's does. The copy is checked against the real
parser once on the box at setup (runbooks/dispatch.md, "hermes-harness as a
task target", step 5). These tests then keep the map true as the repository
changes: a new top-level directory without a row, a renamed directory, two rows
claiming one file, or a table fleet would read only part of all fail here.
"""
import re
import subprocess
import tomllib
from pathlib import Path

import pytest

import tasklib

ROOT = Path(__file__).resolve().parents[2]
# fleet/src/fleet/labels.py::AREA_RE. file_task refuses the whole map if one area breaks it.
AREA_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
# fleet/src/fleet/policy.py::_CELL and _TICKS.
_CELL = re.compile(r"^\s*\|(.+)\|\s*$")
_TICKS = re.compile(r"^`(.*)`$")
# fleet never matches a glob against a path, so this suite is the only definition
# of one. Two forms: `dir/**` is everything under dir; anything else is one file.
GLOB_FORM = re.compile(r"^[^*?\[\]]+(/\*\*)?$")


def policy() -> dict:
    return tomllib.loads((ROOT / "fleet.toml").read_text())["policy"]


def _unticked(cell: str) -> str:
    m = _TICKS.match(cell.strip())
    return m.group(1) if m else cell.strip()


def ownership_rows() -> list[tuple[str, str]]:
    """(glob, area) as fleet reads them: a line that is not `| ... |` ends the table,
    a second `Glob | Area` header starts the rows again, and header cells are compared
    as written (backticks and all)."""
    rows: list[list[str]] = []
    in_table = False
    for line in (ROOT / policy()["ownership"]).read_text().splitlines():
        m = _CELL.match(line)
        if not m:
            in_table = False
            continue
        cells = [c.strip() for c in m.group(1).split("|")]
        if tuple(c.lower() for c in cells[:2]) == ("glob", "area"):
            in_table, rows = True, []
            continue
        if not in_table or all(set(c) <= {"-", ":"} for c in cells if c):
            continue
        rows.append(cells)
    assert all(len(cells) >= 2 for cells in rows), "fleet refuses an ownership row with one cell"
    return [(_unticked(cells[0]), _unticked(cells[1])) for cells in rows]


def matches(path: str, glob: str) -> bool:
    if glob.endswith("/**"):
        return path.startswith(glob[:-2])
    return path == glob


def tracked() -> list[str]:
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True, text=True, check=True)
    return [p for p in out.stdout.split("\0") if p]


def test_fleet_toml_names_this_repository():
    assert tasklib.slug(ROOT) == "FibonAdithya/hermes-harness"


def test_fleet_toml_has_every_policy_key_fleet_indexes():
    # fleet/src/fleet/config.py load_config reads all four with policy[...]; a missing
    # one is a KeyError, and file_task then refuses this repository.
    assert set(policy()) >= {"ownership", "task_classes", "roles", "schemas"}


def test_every_tracked_file_has_exactly_one_area():
    rows = ownership_rows()
    wrong = {}
    for path in tracked():
        areas = sorted({area for glob, area in rows if matches(path, glob)})
        if len(areas) != 1:
            wrong[path] = areas
    assert wrong == {}, f"files with no area or more than one, by docs/agent/ownership.md: {wrong}"


def test_every_glob_is_a_directory_or_one_file():
    odd = [glob for glob, _ in ownership_rows() if not GLOB_FORM.fullmatch(glob)]
    assert odd == []


def test_every_glob_still_matches_a_tracked_file():
    files = tracked()
    dead = [glob for glob, _ in ownership_rows() if not any(matches(p, glob) for p in files)]
    assert dead == []


def test_area_names_are_ones_fleet_accepts():
    bad = sorted({area for _, area in ownership_rows() if not AREA_RE.fullmatch(area)})
    assert bad == []


def test_fleet_cannot_be_started_on_this_repository():
    # Solo only until the task classes, role prompts and packet schemas exist. Adding a
    # prompt file alone would let `task` and run_fleet through to a policy fleet cannot load.
    with pytest.raises(tasklib.TaskError, match="solo only"):
        tasklib.require_role_prompts(ROOT)
