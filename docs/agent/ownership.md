# Ownership map

The Area column is the list of areas a task in this repository can be filed
under. The box's `file_task` verb reads it with fleet's own parser and refuses
any other area. File a task under the area that holds most of the change. Tests
live in the area of the code they cover.

A glob is either `dir/**`, meaning everything under that directory, or the path
of one file. Every row needs its closing `|`, and the table must stay one table:
fleet stops reading at the first line that is not a table row.

`box/tests/test_fleet_target.py` fails when a tracked file has no area or more
than one, when a glob matches nothing or is in another form, or when fleet
would read only part of the table. A new top-level directory needs a row here
in the same pull request.

| Glob | Area |
|---|---|
| `box/**` | box |
| `broker/**` | broker |
| `droplet/**` | droplet |
| `executor/**` | executor |
| `friends/**` | friends |
| `tuner/**` | tuner |
| `wiki/**` | wiki |
| `docs/**` | docs |
| `runbooks/**` | docs |
| `.github/**` | repo |
| `.gitignore` | repo |
| `fleet.toml` | repo |

## Non-dispatchable paths

No task in this repository runs under fleet. Reply `solo` or `drop`.

| Path | Why |
|---|---|
| `**` | fleet has no task classes, role prompts or packet schemas for this repository, so a fleet night here would stop at policy load. The box refuses `task` and `run_fleet` for it. |

Deciding which paths fleet may change unattended (the approvals listener, the
dispatcher, the deploy verb, the executor image and the systemd units are the
obvious exclusions) is part of writing that policy layer, and has not been done.
