import subprocess

from hermes_broker import ssh


def test_run_ssh_quotes_argv_as_one_remote_command(monkeypatch):
    """ssh joins its words with spaces and hands them to the remote login shell:
    ["python", "-c", "print(1)"] would arrive as `python -c print(1)`. Quote it."""
    seen = {}

    def fake_run(command, **kw):
        seen["command"] = command
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ssh.run_ssh("tig-gpu", ["python", "-c", "print(1)", "a b", "x;id"], timeout=5)
    remote = seen["command"][seen["command"].index("--") + 1:]
    assert remote == ["python -c 'print(1)' 'a b' 'x;id'"]


def test_run_ssh_leaves_a_bare_verb_bare(monkeypatch):
    seen = {}

    def fake_run(command, **kw):
        seen["command"] = command
        return subprocess.CompletedProcess(command, 0, "{}", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ssh.run_ssh("tig-server", ["status"], timeout=5, stdin_text="{}")
    assert seen["command"][-1] == "status"
