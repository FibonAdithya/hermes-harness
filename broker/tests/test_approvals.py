from hermes_broker.approvals import parse_command

OWNER = 111222333


def msg(text, **extra):
    message = {"message_id": 1, "from": {"id": OWNER}, "chat": {"id": OWNER}, "text": text}
    message.update(extra)
    return {"update_id": 9, "message": message}


def test_approve_is_parsed():
    assert parse_command(msg("approve 7391"), OWNER) == ("approve", "7391")


def test_approve_is_case_insensitive_and_trims():
    assert parse_command(msg("  Approve 7391 "), OWNER) == ("approve", "7391")


def test_revoke_is_parsed():
    assert parse_command(msg("revoke"), OWNER) == ("revoke", "")


def test_other_text_ignored():
    assert parse_command(msg("hello"), OWNER) is None
    assert parse_command(msg("approve"), OWNER) is None
    assert parse_command(msg("approve 73911"), OWNER) is None
    assert parse_command(msg("approve abcd"), OWNER) is None


def test_non_owner_ignored():
    update = msg("approve 7391")
    update["message"]["from"]["id"] = 999
    assert parse_command(update, OWNER) is None


def test_forwarded_message_ignored():
    """The injection road of As-built #20: someone else's text, forwarded in."""
    assert parse_command(msg("approve 7391", forward_origin={"type": "user"}), OWNER) is None


def test_legacy_forward_fields_ignored():
    assert parse_command(msg("approve 7391", forward_from={"id": 5}), OWNER) is None
    assert parse_command(msg("approve 7391", forward_sender_name="Someone"), OWNER) is None
    assert parse_command(msg("approve 7391", forward_date=123456), OWNER) is None


def test_edited_message_ignored():
    assert parse_command({"update_id": 9, "edited_message": {"text": "approve 7391"}}, OWNER) is None


def test_non_message_update_ignored():
    assert parse_command({"update_id": 9, "callback_query": {"data": "approve 7391"}}, OWNER) is None


def test_missing_text_ignored():
    update = msg("approve 7391")
    del update["message"]["text"]
    assert parse_command(update, OWNER) is None


def test_deploy_is_parsed_lowercased():
    # amended 2026-09-26 in review: arg is now "target:prefix"; a bare deploy defaults to harness
    assert parse_command(msg("deploy 91a2e06"), OWNER) == ("deploy", "harness:91a2e06")
    assert parse_command(msg(" Deploy 91A2E066C768 "), OWNER) == ("deploy", "harness:91a2e066c768")


def test_deploy_needs_seven_to_forty_hex():
    assert parse_command(msg("deploy 91a2e0"), OWNER) is None
    assert parse_command(msg("deploy " + "a" * 41), OWNER) is None
    assert parse_command(msg("deploy master"), OWNER) is None
    assert parse_command(msg("deploy"), OWNER) is None


def test_deploy_from_non_owner_or_forwarded_ignored():
    update = msg("deploy 91a2e06")
    update["message"]["from"]["id"] = 999
    assert parse_command(update, OWNER) is None
    assert parse_command(msg("deploy 91a2e06", forward_origin={"type": "user"}), OWNER) is None


def test_deploy_reply_names_the_full_commit(tmp_path):
    from hermes_broker.approvals import handle
    from hermes_broker.grants import GrantStore
    store = GrantStore(tmp_path / "grants.json")
    sha = "91a2e066c76837d9ac80243755ebc628993a0454"
    store.create_deploy_request(sha, now=1000.0)
    # amended 2026-09-26 in review: mismatches now name the target being asked about
    assert handle(store, "deploy", "0000000", now=1001.0) == "No pending harness deploy of a commit starting 0000000."
    assert handle(store, "deploy", "91a2e06", now=1002.0) == f"Deploy of {sha} to tig-server approved for 10 min."
    assert store.take_deploy_grant(now=1003.0) == sha


def test_approve_and_revoke_replies_unchanged(tmp_path):
    from hermes_broker.approvals import handle
    from hermes_broker.grants import GrantStore
    store = GrantStore(tmp_path / "grants.json")
    code = store.create_request("tig-server", 30, "fix parser", now=1000.0)
    assert handle(store, "approve", "0000" if code != "0000" else "0001", now=1001.0) == "No pending request with that code."
    assert handle(store, "approve", code, now=1001.0) == "Granted tig-server for 30 min.\nFor: fix parser"
    assert handle(store, "revoke", "", now=1002.0) == "Revoked. All boxes locked."
    assert not store.is_active("tig-server", now=1003.0)


import pytest


@pytest.mark.parametrize("verb", ["task", "solo", "drop"])
def test_task_replies_are_parsed(verb):
    assert parse_command(msg(f" {verb.upper()} 7310 "), OWNER) == (verb, "7310")


@pytest.mark.parametrize("text", ["task", "task 731", "task 73100", "solo abcd", "drop 7310 now", "tasks 7310"])
def test_malformed_task_replies_ignored(text):
    assert parse_command(msg(text), OWNER) is None


def test_task_reply_forwarded_or_from_someone_else_ignored():
    assert parse_command(msg("task 7310", forward_origin={"type": "user"}), OWNER) is None
    update = msg("task 7310")
    update["message"]["from"]["id"] = 999
    assert parse_command(update, OWNER) is None


def test_grant_approval_is_judged_by_when_the_owner_sent_it_not_processing_time(tmp_path):
    """REQUEST_TTL_SECONDS is 120s, same as the announcer's list_triage timeout: a
    tick that hangs must not make a timely reply look late."""
    from hermes_broker.approvals import _dispatch
    from hermes_broker.grants import GrantStore
    store = GrantStore(tmp_path / "grants.json")
    code = store.create_request("tig-server", 30, "fix parser", now=1000.0)
    update = msg(f"approve {code}", date=1060)
    # Processing happens at 1200 -- 200s after the request was made, well past its
    # 120s TTL -- but the owner's message was sent at 1060, only 60s in.
    out = _dispatch(update, OWNER, store, None, None, (), now_fn=lambda: 1200.0)
    assert out == "Granted tig-server for 30 min.\nFor: fix parser"


def test_task_reply_exception_is_caught_and_reported(tmp_path, monkeypatch):
    """A broken handle_task (box returns something unexpected) must not kill the loop."""
    import hermes_broker.approvals as a
    from hermes_broker.grants import GrantStore
    from hermes_broker.tasks import TaskStore

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(a, "handle_task", boom)
    store = GrantStore(tmp_path / "grants.json")
    tasks = TaskStore(tmp_path / "tasks.json")
    update = msg("task 7310")
    out = a._dispatch(update, OWNER, store, tasks, lambda *args, **kwargs: {}, (), now_fn=lambda: 2.0)
    assert out == "task reply failed: boom"


def test_dispatch_routes_task_verbs_to_handle_task_and_others_to_handle(tmp_path, monkeypatch):
    import hermes_broker.approvals as a
    from hermes_broker.grants import GrantStore
    from hermes_broker.tasks import TaskStore

    calls = []
    monkeypatch.setattr(a, "handle_task", lambda *args, **kwargs: calls.append("handle_task") or "ok-task")
    monkeypatch.setattr(a, "handle", lambda *args, **kwargs: calls.append("handle") or "ok-approve")
    store = GrantStore(tmp_path / "grants.json")
    tasks = TaskStore(tmp_path / "tasks.json")

    out_task = a._dispatch(msg("task 1234"), OWNER, store, tasks, lambda *args, **kwargs: {}, (), now_fn=lambda: 1.0)
    out_approve = a._dispatch(msg("approve 1234"), OWNER, store, tasks, lambda *args, **kwargs: {}, (), now_fn=lambda: 1.0)

    assert calls == ["handle_task", "handle"]
    assert out_task == "ok-task" and out_approve == "ok-approve"


def test_fleet_deploy_reply_names_fleet(tmp_path):
    from hermes_broker.approvals import handle
    from hermes_broker.grants import GrantStore
    store = GrantStore(tmp_path / "g.json")
    sha = "91a2e066c76837d9ac80243755ebc628993a0454"
    store.create_deploy_request(sha, now=1000.0, target="fleet")
    # amended 2026-09-26 in review: the owner must now name the target; input changed, output did not
    assert handle(store, "deploy", "fleet:91a2e06", now=1002.0) == f"Deploy of fleet {sha} to tig-server approved for 10 min."


def test_bare_deploy_reply_does_not_approve_a_fleet_request(tmp_path):
    """Fix round 1, item 1: a fleet request must not be approvable by an owner who typed a
    bare `deploy <prefix>` believing it names a harness commit -- fleet main has no branch
    protection, so a misread approval would be unrecoverable."""
    from hermes_broker.approvals import handle
    from hermes_broker.grants import GrantStore
    store = GrantStore(tmp_path / "g.json")
    sha = "91a2e066c76837d9ac80243755ebc628993a0454"
    store.create_deploy_request(sha, now=1000.0, target="fleet")
    assert handle(store, "deploy", "91a2e06", now=1001.0) == "No pending harness deploy of a commit starting 91a2e06."
    # the request is still pending: naming the target now grants it
    assert handle(store, "deploy", "fleet:91a2e06", now=1002.0) == f"Deploy of fleet {sha} to tig-server approved for 10 min."


def test_deploy_fleet_reply_does_not_approve_a_harness_request(tmp_path):
    from hermes_broker.approvals import handle
    from hermes_broker.grants import GrantStore
    store = GrantStore(tmp_path / "g.json")
    sha = "91a2e066c76837d9ac80243755ebc628993a0454"
    store.create_deploy_request(sha, now=1000.0, target="harness")
    assert handle(store, "deploy", "fleet:91a2e06", now=1001.0) == "No pending fleet deploy of a commit starting 91a2e06."
    assert handle(store, "deploy", "91a2e06", now=1002.0) == f"Deploy of {sha} to tig-server approved for 10 min."


@pytest.mark.parametrize("text, expected", [
    ("deploy fleet abc1234", ("deploy", "fleet:abc1234")),
    ("deploy harness abc1234", ("deploy", "harness:abc1234")),
    ("deploy abc1234", ("deploy", "harness:abc1234")),
    ("Deploy FLEET ABC1234", ("deploy", "fleet:abc1234")),
])
def test_deploy_reply_parses_the_named_target(text, expected):
    assert parse_command(msg(text), OWNER) == expected


def test_deploy_with_unknown_target_word_ignored():
    assert parse_command(msg("deploy droplet abc1234"), OWNER) is None
