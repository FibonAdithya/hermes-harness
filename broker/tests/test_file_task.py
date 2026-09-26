"""file_task is ungated and can only produce a triage issue."""
import pytest


@pytest.fixture
def server(monkeypatch):
    from hermes_broker import server as s
    calls = []
    monkeypatch.setattr(s, "_target", lambda box: "tig-server")
    monkeypatch.setattr(s.box, "call", lambda t, v, a, timeout=60: calls.append((v, a)) or
                        {"repo": "fleet", "slug": "FibonAdithya/fleet", "number": 9,
                         "url": "https://github.com/FibonAdithya/fleet/issues/9"})
    return s, calls


def test_file_task_needs_no_grant_and_passes_only_its_arguments(server):
    s, calls = server
    out = s.file_task("fleet", "t", "b", "daemon", "investigation")
    assert calls == [("file_task", {"repo": "fleet", "title": "t", "body": "b", "area": "daemon", "cls": "investigation"})]
    assert "fleet#9" in out and "owner" in out


def test_the_tool_cannot_reach_the_owner_only_verbs():
    """Catches an MCP tool being added for approve_task or close_task."""
    import inspect
    from hermes_broker import server as s
    src = inspect.getsource(s)
    assert '"approve_task"' not in src and '"close_task"' not in src
