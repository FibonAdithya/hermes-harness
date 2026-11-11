# Ownership map

The Area column is the list of areas a task in this repository can be filed
under. The box's `file_task` verb reads it with fleet's own parser and refuses
any other area. File a task under the area that holds most of the change. Tests
live in the area of the code they cover.

`box/tests/test_fleet_target.py` fails when a tracked file has no area or more
than one, or when a glob no longer matches anything. A new top-level directory
needs a row here in the same pull request.

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

No task in this repository is approved with `task`. Reply `solo` or `drop`.

| Path | Why |
|---|---|
| `**` | fleet has no task classes, role prompts or packet schemas for this repository, so a fleet night here stops at policy load. A `task` reply would leave the issue `fleet:ready` with nothing able to run it. |

Deciding which paths fleet may change unattended (the approvals listener, the
dispatcher, the deploy verb, the executor image and the systemd units are the
obvious exclusions) is part of writing that policy layer, and has not been done.
