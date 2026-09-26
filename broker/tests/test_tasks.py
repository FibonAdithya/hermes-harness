"""Task codes: one per announced issue, bound to the text the owner was shown."""
import os
import stat

import pytest
from hermes_broker.tasks import CODE_TTL_SECONDS, TaskStore

H1, H2 = "a" * 64, "b" * 64


@pytest.fixture
def store(tmp_path):
    return TaskStore(tmp_path / "tasks.json")


def test_first_announcement_gets_a_four_digit_code(store):
    code = store.announce("fleet", "FibonAdithya/fleet", 42, H1, now=1000.0, avoid=set())
    assert code is not None and len(code) == 4 and code.isdigit()


def test_the_same_issue_and_text_is_not_announced_twice(store):
    store.announce("fleet", "FibonAdithya/fleet", 42, H1, now=1000.0, avoid=set())
    assert store.announce("fleet", "FibonAdithya/fleet", 42, H1, now=1300.0, avoid=set()) is None


def test_changed_text_gets_a_new_code_and_kills_the_old(store):
    old = store.announce("fleet", "FibonAdithya/fleet", 42, H1, now=1000.0, avoid=set())
    new = store.announce("fleet", "FibonAdithya/fleet", 42, H2, now=1300.0, avoid=set())
    assert new is not None and new != old
    assert store.take(old, now=1301.0) is None
    assert store.take(new, now=1301.0)["hash"] == H2


def test_take_is_single_use(store):
    code = store.announce("fleet", "FibonAdithya/fleet", 42, H1, now=1000.0, avoid=set())
    assert store.take(code, now=1001.0) == {"repo": "fleet", "slug": "FibonAdithya/fleet", "number": 42, "hash": H1}
    assert store.take(code, now=1002.0) is None


def test_codes_expire_after_seven_days(store):
    code = store.announce("fleet", "FibonAdithya/fleet", 42, H1, now=1000.0, avoid=set())
    assert store.take(code, now=1000.0 + CODE_TTL_SECONDS) is None


def test_expired_issue_is_announced_again(store):
    store.announce("fleet", "FibonAdithya/fleet", 42, H1, now=1000.0, avoid=set())
    assert store.announce("fleet", "FibonAdithya/fleet", 42, H1, now=1000.0 + CODE_TTL_SECONDS, avoid=set()) is not None


def test_avoided_codes_are_never_issued(store, monkeypatch):
    import hermes_broker.tasks as tasks
    draws = iter([1234, 1234, 5678])
    monkeypatch.setattr(tasks.secrets, "randbelow", lambda n: next(draws))
    assert store.announce("fleet", "FibonAdithya/fleet", 1, H1, now=1.0, avoid={"1234"}) == "5678"


def test_prune_drops_issues_no_longer_in_triage_only_for_repos_listed(store):
    """Catches pruning every repo when one repo's listing failed: its issues
    would all be re-announced on the next tick."""
    a = store.announce("fleet", "FibonAdithya/fleet", 1, H1, now=1.0, avoid=set())
    b = store.announce("fleet-fixture", "FibonAdithya/fleet-fixture", 2, H1, now=1.0, avoid=set())
    store.prune(listed_repos={"fleet"}, present=set(), now=2.0)
    assert store.take(a, now=3.0) is None
    assert store.take(b, now=3.0) is not None


def test_codes_survive_a_listener_restart(tmp_path):
    code = TaskStore(tmp_path / "tasks.json").announce("fleet", "FibonAdithya/fleet", 7, H1, now=1.0, avoid=set())
    assert TaskStore(tmp_path / "tasks.json").take(code, now=2.0)["number"] == 7


def test_file_is_owner_only(store):
    store.announce("fleet", "FibonAdithya/fleet", 7, H1, now=1.0, avoid=set())
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600


def test_live_codes(store):
    code = store.announce("fleet", "FibonAdithya/fleet", 7, H1, now=1.0, avoid=set())
    assert store.live_codes(now=2.0) == {code}
    assert store.live_codes(now=2.0 + CODE_TTL_SECONDS) == set()
