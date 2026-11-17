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


# --- chaind (box/units/chaind.service, box/chain-config.example.toml) -------
import tomllib

CHAIND = REPO / "box" / "units" / "chaind.service"
CHAIN_CFG = REPO / "box" / "chain-config.example.toml"


def _section(path, name):
    """Key/value lines of one [section]; a small parser, not a substring match."""
    out, cur = {}, None
    for line in path.read_text().splitlines():
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            cur = s[1:-1]
        elif cur == name and "=" in s and not s.startswith("#"):
            k, v = s.split("=", 1)
            out.setdefault(k.strip(), []).append(v.strip())
    return out


def test_box_chaind_unit_kill_mode_process():
    svc = _section(CHAIND, "Service")
    assert svc["KillMode"] == ["process"]
    # Spec section 6: chaind is always on, so it comes back after a clean exit too, and
    # the 10 s spacing keeps a crash loop under systemd's default start limit (5 in 10 s).
    assert svc["Restart"] == ["always"]
    assert svc["RestartSec"] == ["10"]
    assert svc["WorkingDirectory"] == ["%h/chain"]
    assert svc["ExecStart"] == ["%h/chain/.venv/bin/chaind"]
    assert _section(CHAIND, "Install")["WantedBy"] == ["default.target"]


def test_box_chaind_unit_path_matches_dispatch():
    m = re.search(r'"PATH":\s*f"([^"]+)"', (REPO / "box" / "dispatch").read_text())
    assert m, "PATH f-string not found in box/dispatch"
    want = m.group(1).replace("{HOME}", "%h")
    assert _section(CHAIND, "Service")["Environment"] == [f"PATH={want}"]


def test_box_chaind_unit_has_no_ssh_agent_or_env_file():
    svc = _section(CHAIND, "Service")
    assert "EnvironmentFile" not in svc
    assert "SSH_AUTH_SOCK" not in CHAIND.read_text()


def test_chain_example_config_has_no_intake():
    assert "intake" not in tomllib.loads(CHAIN_CFG.read_text())


def test_chain_example_budgets_fit_ceiling():
    cfg = tomllib.loads(CHAIN_CFG.read_text())
    names = ["confirm", "plan", "audit", "implement", "pr", "review", "fix"]
    assert set(cfg["stages"]) == set(names)
    # chain counts review and fix spend across all their rounds against the one stage
    # budget, so a run's ceiling is the plain sum of the seven budgets.
    total = sum(cfg["stages"][n]["budget_usd"] for n in names)
    assert total <= 40.0, total


def test_install_survives_chaind_failing_to_start(tmp_path):
    """A crash-looping chaind must not abort a harness install (set -e)."""
    import os
    import subprocess
    home, fake = tmp_path / "home", tmp_path / "bin"
    (home / "chain" / ".venv" / "bin").mkdir(parents=True)
    (home / "chain" / ".venv" / "bin" / "chaind").write_text("#!/bin/sh\n")
    (home / "chain" / ".venv" / "bin" / "chaind").chmod(0o755)
    fake.mkdir()
    log = tmp_path / "systemctl.log"
    for name, body in (("systemctl", f'echo "$*" >> "{log}"\ncase "$*" in *chaind*) exit 1;; esac\n'),
                       ("docker", "exit 0\n"), ("uv", "exit 0\n")):
        (fake / name).write_text("#!/bin/sh\n" + body)
        (fake / name).chmod(0o755)
    env = {"HOME": str(home), "PATH": f"{fake}:{os.environ['PATH']}", "UV": str(fake / "uv")}
    p = subprocess.run(["bash", str(REPO / "box" / "install.sh")], env=env, capture_output=True, text=True,
                       timeout=60, check=False)
    assert p.returncode == 0, p.stderr
    assert "--user enable --now chaind.service" in log.read_text()
    assert "chaind failed to start; see journalctl --user -u chaind" in p.stderr
    assert "installed verbs:" in p.stdout
