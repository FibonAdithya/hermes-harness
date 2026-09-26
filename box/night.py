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
    return json.dumps(v, ensure_ascii=False)  # a JSON string literal is a valid TOML basic string


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


def _record(root: Path, kind: str, status: str, why: str, exit_code: int) -> None:
    d = boxlib.night_dir(root, boxlib.new_night_id(kind))
    d.mkdir(parents=True)
    boxlib.record_start(d, {"kind": kind, "timer": True})
    (d / "log").write_text(why + "\n")
    boxlib.write_status(d, status, exit_code=exit_code)


def _skip(root: Path, kind: str, why: str) -> None:
    _record(root, kind, "skipped", why, 0)


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def main(argv: list[str]) -> int:
    kind = argv[1] if len(argv) > 1 else ""
    env = dict(os.environ)
    root = boxlib.nights_root(env)
    if kind not in ("fleet", "talos"):
        print(json.dumps({"error": f"usage: night.py fleet|talos, got {kind!r}"}))
        return 2
    try:
        return _start(kind, env, root)
    except Exception as exc:  # a bad config must be a failed night, not a silent journal line
        _record(root, kind, "failed", f"could not start the {kind} night from {root / 'config.toml'}: {exc!r}", 1)
        print(json.dumps({"error": f"{kind} night not started: {exc}"}))
        return 1


def _start(kind: str, env: dict, root: Path) -> int:
    home = Path(env.get("HOME", "/home/adi"))
    path = root / "config.toml"
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
        backend = t.get("backend", "local")
        if backend in boxlib.TALOS_PAID_BACKENDS:
            raise ValueError(f"backend {backend!r} bills per job; start it with an approved run_talos, not a timer")
        workdir, cmd, secs = boxlib.talos_command(home, entry["challenge"], entry["direction"],
                                                  int(t.get("iterations", 30)), backend,
                                                  t.get("compute_usd", boxlib.DEFAULT_TALOS_COMPUTE_USD))
        _write_atomic(path, dump_toml(rest))
        night_id = boxlib.start_night("talos", workdir, cmd, secs, env, {**entry, "backend": backend, "timer": True})
    print(json.dumps({"id": night_id}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
