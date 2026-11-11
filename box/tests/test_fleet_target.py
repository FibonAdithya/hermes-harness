"""hermes-harness as a task target: fleet.toml and the ownership map that file_task reads.

fleet is not installed where this suite runs, so fleet's own parser reading the
map is checked by hand at deploy time (runbooks/dispatch.md, "hermes-harness as
a task target"). These tests cover the part the box's own code reads (the slug)
and keep the map true as the repository changes: a new top-level directory
without a row, a renamed directory, or two rows claiming one file all fail here.
"""
import fnmatch
import re
import subprocess
import tomllib
from pathlib import Path

import tasklib

ROOT = Path(__file__).resolve().parents[2]
# fleet/src/fleet/labels.py::AREA_RE. file_task refuses the whole map if one area breaks it.
AREA_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")


def ownership_rows() -> list[tuple[str, str]]:
    """(glob, area) from the table headed Glob | Area, and from no other table."""
    policy = tomllib.loads((ROOT / "fleet.toml").read_text())["policy"]
    rows, in_table = [], False
    for line in (ROOT / policy["ownership"]).read_text().splitlines():
        if not line.strip().startswith("|"):
            in_table = False
            continue
        cells = [c.strip().strip("`") for c in line.strip().strip("|").split("|")]
        if [c.lower() for c in cells[:2]] == ["glob", "area"]:
            in_table = True
        elif in_table and not all(set(c) <= {"-", ":"} for c in cells):
            rows.append((cells[0], cells[1]))
    return rows


def matches(path: str, glob: str) -> bool:
    if glob.endswith("/**"):
        return path.startswith(glob[:-2])
    return fnmatch.fnmatchcase(path, glob)


def tracked() -> list[str]:
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files"], capture_output=True, text=True, check=True)
    return out.stdout.splitlines()


def test_fleet_toml_names_this_repository():
    assert tasklib.slug(ROOT) == "FibonAdithya/hermes-harness"


def test_every_tracked_file_has_exactly_one_area():
    rows = ownership_rows()
    wrong = {}
    for path in tracked():
        areas = sorted({area for glob, area in rows if matches(path, glob)})
        if len(areas) != 1:
            wrong[path] = areas
    assert wrong == {}, f"files with no area or more than one, by docs/agent/ownership.md: {wrong}"


def test_every_glob_still_matches_a_tracked_file():
    files = tracked()
    dead = [glob for glob, _ in ownership_rows() if not any(matches(p, glob) for p in files)]
    assert dead == []


def test_area_names_are_ones_fleet_accepts():
    bad = sorted({area for _, area in ownership_rows() if not AREA_RE.fullmatch(area)})
    assert bad == []
