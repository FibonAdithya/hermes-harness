#!/usr/bin/env python3
"""Timer entry point: start tonight's fleet or Talos run from ~/nights/config.toml.

Takes no input from anywhere but that file. An empty Talos queue records a
`skipped` night and exits 0, so a quiet night is not an alert.
"""
from __future__ import annotations

import json
import os
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import boxlib  # noqa: E402


def pop_talos(cfg: dict) -> tuple[dict | None, dict]:
    talos = dict(cfg.get("talos", {}))
    queue = list(talos.get("queue", []))
    if not queue:
        return None, cfg
    entry, rest = queue[0], queue[1:]
    out = {**cfg, "talos": {**talos, "queue": rest}}
    return entry, out


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return json.dumps(v)  # a JSON string literal is a valid TOML basic string


def dump_toml(cfg: dict) -> str:
    """Enough TOML for this file: flat tables and one array of tables."""
    lines: list[str] = []
    for table, body in cfg.items():
        scalars = {k: v for k, v in body.items() if not isinstance(v, list)}
        lines.append(f"[{table}]")
        for k, v in scalars.items():
            lines.append(f"{k} = {_toml_value(v)}")
        lines.append("")
        for k, v in body.items():
            if isinstance(v, list):
                for item in v:
                    lines.append(f"[[{table}.{k}]]")
                    for ik, iv in item.items():
                        lines.append(f"{ik} = {_toml_value(iv)}")
                    lines.append("")
    return "\n".join(lines)


def _skip(root: Path, kind: str, why: str) -> None:
    d = boxlib.night_dir(root, boxlib.new_night_id(kind))
    d.mkdir(parents=True)
    (d / "log").write_text(why + "\n")
    boxlib.write_status(d, "skipped", exit_code=0)


def main(argv: list[str]) -> int:
    kind = argv[1] if len(argv) > 1 else ""
    env = dict(os.environ)
    home = Path(env.get("HOME", "/home/adi"))
    root = boxlib.nights_root(env)
    path = root / "config.toml"
    if kind not in ("fleet", "talos"):
        print(json.dumps({"error": f"usage: night.py fleet|talos, got {kind!r}"}))
        return 2
    if not path.is_file():
        _skip(root, kind, f"no {path}")
        return 0
    cfg = tomllib.loads(path.read_text())
    if kind == "fleet":
        f = cfg.get("fleet", {})
        workdir, cmd, secs = boxlib.fleet_command(home, f.get("repo", ""), int(f.get("hours", 8)))
        night_id = boxlib.start_night("fleet", workdir, cmd, secs, env, {"repo": workdir.name, "timer": True})
    else:
        entry, rest = pop_talos(cfg)
        if entry is None:
            _skip(root, "talos", "talos queue is empty")
            return 0
        t = cfg.get("talos", {})
        workdir, cmd, secs = boxlib.talos_command(home, entry["challenge"], entry["direction"],
                                                  int(t.get("iterations", 30)), t.get("backend", "local"))
        path.write_text(dump_toml(rest))
        night_id = boxlib.start_night("talos", workdir, cmd, secs, env, {**entry, "timer": True})
    print(json.dumps({"id": night_id}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
