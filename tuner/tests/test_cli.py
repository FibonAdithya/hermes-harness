"""CLI: `scrub --until` and `replay-check` (plan Task 12), on synthetic state.dbs."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from factory import FLAT, make_db
from hermes_tuner import policy, scrub
from hermes_tuner.__main__ import main
from hermes_tuner.bridge import FakeBridge
from hermes_tuner.history import History
from hermes_tuner.replay import search_reducer
from test_relay import RUNNER, relay

CAL = "cc68e24f21cd"
GET = "mcp__google__get_gmail_message_content"
SEARCH = "mcp__google__search_gmail_messages"
EVENTS = "mcp__google__get_events"
PROMPT = "Sync the calendar from mail."
OWNER = "priya.raman.private@example.test"
# A used, non-meeting URL host of 20+ chars: the search keeps it (keep_hosts).
HOST = "quarterly-planning-portal.example.test"
TOOL_NAME = "tuned_batch_fetch_contents"


def _ts(day: str, hour: int = 6) -> float:
    return datetime.strptime(day, "%Y-%m-%d").replace(hour=hour, tzinfo=timezone.utc).timestamp()


def _sid(day: str, job: str = CAL) -> str:
    return f"cron_{job}_{day.replace('-', '')}_060000"


def _email(i: int, k: int) -> tuple[str, str]:
    """(made-up email text, what the model quotes from it)."""
    quote = (f"quarterly planning offsite for Lumenfield Orchards moves to Thursday "
             f"the {i}{k}th at the Harborview annex, agenda https://{HOST}/agenda/{7781 + 10 * i + k}")
    tracking = " ".join(f"https://click.tracking-mailer-domain.example.test/ls/click?upn=aZ9{i}{k}{n}"
                        + "Qx7" * 30 for n in range(20))
    text = (f"Hi team, the {quote} . Please bring the signed vendor contracts for the "
            f"orchard expansion. {tracking} Regards, Priya Raman, Lumenfield Orchards")
    return text, quote


def _result(text: str) -> str:
    return json.dumps({"result": text, "structuredContent": {"result": text}}, ensure_ascii=False)


def _args(i: int, k: int) -> dict:
    return {"message_id": f"msg-{i}-{k}-lumenfield-orchards-vendor-q4", "user_google_email": OWNER}


DAYS = ["2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23"]


def _db(tmp_path):
    """Four calendar-sync runs; each fetches two mails in one turn and quotes both."""
    secrets: list[str] = [OWNER]
    sessions = []
    for i, day in enumerate(DAYS):
        calls, quotes = [], []
        for k in range(2):
            text, quote = _email(i, k)
            calls.append((GET, _args(i, k), _result(text), 1))
            quotes.append(quote)
            secrets += [text, quote, *_args(i, k).values()]
        final = "Added to the calendar: " + "; ".join(quotes)
        secrets.append(final)
        sessions.append({"id": _sid(day), "source": "cron", "started_at": _ts(day),
                         "calls": calls, "final_content": [final]})
    return make_db(tmp_path / "state.db", sessions), secrets


def _tool_dir(tmp_path, replay_inputs: bool = False):
    d = tmp_path / "tools" / TOOL_NAME
    d.mkdir(parents=True)
    spec = {"name": TOOL_NAME, "description": "Fetch several messages in one call.",
            "parameters": {"type": "object",
                           "properties": {"message_ids": {"type": "array"}}},
            "calls": [GET], "mutating": False}
    if replay_inputs:
        spec["replay_inputs"] = [{"param": "message_ids", "from_call": GET, "arg": "message_id",
                                  "collect": "list"}]
    (d / "tool.json").write_text(json.dumps(spec))
    # The output holds the mails' full text: replay-check must not print it.
    (d / "program.py").write_text(
        f"def main(args, call):\n"
        f"    return [call({GET!r}, {{'message_id': m}}) for m in args['message_ids']]\n")
    return d


def _pricing(tmp_path):
    p = tmp_path / "price.json"
    p.write_text(json.dumps(FLAT.to_dict()))
    return p


def _bridge():
    return FakeBridge(jobs={CAL: {"id": CAL, "prompt": PROMPT}}, fail={"pricing"})


def _run(argv, tmp_path, bridge=None):
    return main(argv, bridge=bridge if bridge is not None else _bridge(),
                run_program=relay().run_program, replay_argv=lambda name: RUNNER)


def _lines(out: str) -> list[dict]:
    return [json.loads(x) for x in out.splitlines()]


REPLAY_INPUT = f"message_ids={GET}.message_id:list"


# --- replay-check: privacy -----------------------------------------------------------

def test_replay_check_prints_counts_only(tmp_path, capsys):
    """No line of stdout or stderr holds any 20-char piece of a synthetic text or arg
    value, in reducer mode and in composed mode. Catches: printing `spec` (its
    keep_hosts holds HOST), program output, argument values or result text."""
    db, secrets = _db(tmp_path)
    base = ["replay-check", "--job", CAL, "--db", str(db), "--now", "2026-10-29",
            "--pricing", str(_pricing(tmp_path))]
    assert _run(base, tmp_path) == 0
    assert _run(base + ["--composed", str(_tool_dir(tmp_path)),
                        "--replay-input", REPLAY_INPUT], tmp_path) == 0
    cap = capsys.readouterr()
    printed = cap.out + cap.err
    leaks = {s[j:j + 20] for s in secrets for j in range(len(s) - 19) if s[j:j + 20] in printed}
    assert leaks == set()
    assert len(cap.out.splitlines()) == len(policy.REDUCER_TOOLS) + 1


def test_privacy_fixture_is_discriminating(tmp_path):
    """The privacy test's db makes the search keep HOST in the spec, so printing the
    spec would leak a 20-char piece of it (the mutation check relies on this)."""
    db, _ = _db(tmp_path)
    runs = History.open(db).replay_runs(CAL, _ts("2026-10-01"))
    r = search_reducer(runs, GET, CAL, [PROMPT], FLAT)
    assert r.reason == "ok"
    assert HOST in json.dumps(r.spec)


# --- replay-check: reducer mode --------------------------------------------------------

def test_replay_check_reducer_lines(tmp_path, capsys):
    db, _ = _db(tmp_path)
    bridge = _bridge()
    assert _run(["replay-check", "--job", CAL, "--db", str(db), "--now", "2026-10-29",
                 "--pricing", str(_pricing(tmp_path))], tmp_path, bridge) == 0
    lines = {x["tool"]: x for x in _lines(capsys.readouterr().out)}
    assert set(lines) == set(policy.REDUCER_TOOLS)
    g = lines[GET]
    assert set(g) == {"tool", "reason", "sessions", "results", "bytes_before", "bytes_after",
                      "cut", "used_total", "used_kept", "est_saving_per_run", "ops"}
    assert (g["reason"], g["sessions"], g["results"]) == ("ok", 4, 8)
    assert g["cut"] == round(1 - g["bytes_after"] / g["bytes_before"], 4) and g["cut"] >= 0.5
    assert g["used_kept"] == g["used_total"] > 0
    assert "drop_fields" in g["ops"] and "shorten_urls" in g["ops"]
    assert all(isinstance(op, str) for op in g["ops"])
    for t in (SEARCH, EVENTS):
        assert lines[t] == {"tool": t, "reason": "no_runs", "sessions": 0, "results": 0,
                            "bytes_before": 0, "bytes_after": 0, "cut": 0.0, "used_total": 0,
                            "used_kept": 0, "est_saving_per_run": 0.0, "ops": []}
    # The job prompt is excluded as the cycle does; prices come from the file.
    assert bridge.calls == [("get_job", (CAL,))]


def test_replay_check_tool_and_window(tmp_path, capsys):
    """--tool limits the check to one tool; --now/--days set the window
    [now - days, now). Catches: a window that ignores --days or --now."""
    db, _ = _db(tmp_path)
    base = ["replay-check", "--job", CAL, "--db", str(db), "--tool", GET,
            "--pricing", str(_pricing(tmp_path))]
    got = []
    for extra in (["--now", "2026-10-29"],                 # all 4 runs
                  ["--now", "2026-10-29", "--days", "7"],  # 10-22 onwards: 2 runs
                  ["--now", "2026-10-21"],                 # 10-20 only: 1 run
                  ["--now", "2026-10-20"]):                # until is exclusive: none
        assert _run(base + extra, tmp_path) == 0
        (line,) = _lines(capsys.readouterr().out)
        assert line["tool"] == GET
        got.append((line["sessions"], line["reason"]))
    assert got == [(4, "ok"), (2, "too_few_sessions"), (1, "too_few_sessions"), (0, "no_runs")]


def test_replay_check_default_days_is_28(tmp_path, capsys):
    db, _ = _db(tmp_path)
    base = ["replay-check", "--job", CAL, "--db", str(db), "--tool", GET,
            "--pricing", str(_pricing(tmp_path))]
    # now 11-17 00:00 - 28 days = 10-20 00:00: all 4 runs; now 11-18 starts at 10-21.
    for now, sessions in (("2026-11-17", 4), ("2026-11-18", 3)):
        assert _run(base + ["--now", now], tmp_path) == 0
        assert _lines(capsys.readouterr().out)[0]["sessions"] == sessions


def test_replay_check_without_now_uses_the_clock(tmp_path, capsys):
    db, _ = _db(tmp_path)
    argv = ["replay-check", "--job", CAL, "--db", str(db), "--tool", GET,
            "--pricing", str(_pricing(tmp_path))]
    assert main(argv, bridge=_bridge(), now=_ts("2026-10-24", 0)) == 0
    assert _lines(capsys.readouterr().out)[0]["sessions"] == 4
    assert main(argv, bridge=_bridge(), now=_ts("2026-12-30", 0)) == 0
    assert _lines(capsys.readouterr().out)[0]["sessions"] == 0


def test_replay_check_prices_from_the_bridge_without_pricing_file(tmp_path, capsys):
    db, _ = _db(tmp_path)
    bridge = FakeBridge(jobs={CAL: {"id": CAL, "prompt": PROMPT}}, price=FLAT.to_dict())
    assert _run(["replay-check", "--job", CAL, "--db", str(db), "--tool", GET,
                 "--now", "2026-10-29"], tmp_path, bridge) == 0
    assert _lines(capsys.readouterr().out)[0]["reason"] == "ok"
    assert ("pricing", ()) in bridge.calls


def test_replay_check_unusable_db_exits_nonzero(tmp_path, capsys):
    rc = _run(["replay-check", "--job", CAL, "--db", str(tmp_path / "missing.db"),
               "--now", "2026-10-29", "--pricing", str(_pricing(tmp_path))], tmp_path)
    assert rc == 1
    assert capsys.readouterr().out == ""


# --- replay-check: composed mode ------------------------------------------------------

def test_replay_check_composed_line(tmp_path, capsys):
    db, _ = _db(tmp_path)
    assert _run(["replay-check", "--job", CAL, "--db", str(db), "--now", "2026-10-29",
                 "--pricing", str(_pricing(tmp_path)), "--composed", str(_tool_dir(tmp_path)),
                 "--replay-input", REPLAY_INPUT], tmp_path) == 0
    (line,) = _lines(capsys.readouterr().out)
    assert set(line) == {"tool", "reason", "sessions", "limit_hits", "errors", "in_bytes",
                         "out_bytes", "eligible", "saving_per_run", "gate"}
    assert line["tool"] == TOOL_NAME
    assert (line["reason"], line["sessions"], line["limit_hits"], line["errors"],
            line["eligible"]) == ("ok", 4, 0, 0, 4)
    assert line["in_bytes"] > 0 and line["out_bytes"] > 0
    assert line["gate"] is None or isinstance(line["gate"], str)


def test_replay_check_composed_limit_hit(tmp_path, capsys):
    """A program whose output passes the 50,000-byte cap: limit_hits 1 and a gate."""
    db, _ = _db(tmp_path)
    d = _tool_dir(tmp_path, replay_inputs=True)
    (d / "program.py").write_text("def main(args, call):\n    return 'a' * 60000\n")
    assert _run(["replay-check", "--job", CAL, "--db", str(db), "--now", "2026-10-29",
                 "--pricing", str(_pricing(tmp_path)), "--composed", str(d)], tmp_path) == 0
    (line,) = _lines(capsys.readouterr().out)
    assert (line["reason"], line["limit_hits"], line["sessions"]) == ("limit_hit", 1, 1)
    assert line["gate"] == "replay: limit hit in 1 of 1 sessions"


def test_replay_check_replay_input_overrides_and_supplies(tmp_path, capsys):
    """Without replay_inputs in tool.json and no flag, the arguments are unusable;
    tool.json's own entries are used when present."""
    db, _ = _db(tmp_path)
    base = ["replay-check", "--job", CAL, "--db", str(db), "--now", "2026-10-29",
            "--pricing", str(_pricing(tmp_path))]
    with pytest.raises(SystemExit) as e:
        _run(base + ["--composed", str(_tool_dir(tmp_path))], tmp_path)
    assert e.value.code == 2
    capsys.readouterr()
    other = tmp_path / "b"
    assert _run(base + ["--composed", str(_tool_dir(other, replay_inputs=True))], tmp_path) == 0
    assert _lines(capsys.readouterr().out)[0]["sessions"] == 4


@pytest.mark.parametrize("bad", [
    "message_ids=get_gmail_message_content.message_id:list",   # short tool name: not in calls
    f"message_ids={GET}.message_id:some",                       # collect
    f"message_ids={GET}:list",                                  # no arg
    f"{GET}.message_id:list",                                   # no param
    f"other={GET}.message_id:list",                             # not a parameter
])
def test_replay_check_rejects_bad_replay_input(tmp_path, capsys, bad):
    db, _ = _db(tmp_path)
    with pytest.raises(SystemExit) as e:
        _run(["replay-check", "--job", CAL, "--db", str(db), "--now", "2026-10-29",
              "--pricing", str(_pricing(tmp_path)), "--composed", str(_tool_dir(tmp_path)),
              "--replay-input", bad], tmp_path)
    assert e.value.code == 2


def test_replay_input_splits_on_the_last_dot(tmp_path, capsys):
    """`<param>=<tool>.<arg>:<collect>`: the split is on the last dot before the colon."""
    from hermes_tuner.__main__ import _replay_input
    assert _replay_input(f"message_ids={GET}.message_id:first") == {
        "param": "message_ids", "from_call": GET, "arg": "message_id", "collect": "first"}
    assert _replay_input("p=x.y.z:list")["from_call"] == "x.y"


# --- scrub --until ---------------------------------------------------------------------

def test_scrub_until_is_exclusive(tmp_path):
    days = ["2026-10-27", "2026-10-28", "2026-10-29"]
    sessions = [{"id": _sid(d), "source": "cron", "started_at": _ts(d, 0), "calls": []}
                for d in days]
    db = make_db(tmp_path / "state.db", sessions)
    out = tmp_path / "h.json.gz"
    assert main(["scrub", "--db", str(db), "--out", str(out), "--since", "2026-10-28",
                 "--until", "2026-10-29"]) == 0
    assert [s.id for s in scrub.load(out)] == [_sid("2026-10-28")]
    assert main(["scrub", "--db", str(db), "--out", str(out)]) == 0
    assert [s.id for s in scrub.load(out)] == [_sid(d) for d in days]


# --- replay-check: exception paths print the type only ---------------------------------

def _leaks(secret: str, printed: str) -> set[str]:
    return {secret[j:j + 20] for j in range(len(secret) - 19) if secret[j:j + 20] in printed}


def test_replay_check_reducer_error_prints_type_only(tmp_path, capsys):
    """A search that raises with recorded text in its message: the line says
    error:ValueError and the message is not printed. Catches: str(exc) in the line."""
    db, _ = _db(tmp_path)
    secret = _email(0, 0)[0][:120]

    def search(runs, tool, job, exclude, pricing):
        raise ValueError(secret)

    assert main(["replay-check", "--job", CAL, "--db", str(db), "--now", "2026-10-29",
                 "--tool", GET, "--pricing", str(_pricing(tmp_path))],
                bridge=_bridge(), search=search) == 0
    cap = capsys.readouterr()
    (line,) = _lines(cap.out)
    assert line == {"tool": GET, "reason": "error:ValueError"}
    assert _leaks(secret, cap.out + cap.err) == set()


def test_replay_check_composed_error_prints_type_only(tmp_path, capsys):
    db, _ = _db(tmp_path)
    secret = _email(1, 1)[0][:120]

    def run_program(*a, **kw):
        raise ValueError(secret)

    assert main(["replay-check", "--job", CAL, "--db", str(db), "--now", "2026-10-29",
                 "--pricing", str(_pricing(tmp_path)), "--composed", str(_tool_dir(tmp_path)),
                 "--replay-input", REPLAY_INPUT],
                bridge=_bridge(), run_program=run_program, replay_argv=lambda n: RUNNER) == 0
    cap = capsys.readouterr()
    (line,) = _lines(cap.out)
    assert line == {"tool": TOOL_NAME, "reason": "error:ValueError"}
    assert _leaks(secret, cap.out + cap.err) == set()
