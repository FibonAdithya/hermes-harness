import pytest

from hermes_friends.system import System

# The error message includes the command line, so each expected text is assembled by printf
# and never appears literally in the command.


def test_run_failure_shows_stdout_when_stderr_is_empty(tmp_path):
    # Hermes prints errors such as "Profile 'x' already exists" to stdout, not stderr.
    with pytest.raises(RuntimeError, match="Profile already-exists"):
        System(tmp_path)._run(["sh", "-c", "printf 'Profile %s-%s' already exists; exit 1"])


def test_run_failure_shows_stderr(tmp_path):
    with pytest.raises(RuntimeError, match="boom-on-stderr"):
        System(tmp_path)._run(["sh", "-c", "printf '%s-%s-%s' boom on stderr >&2; exit 3"])


def test_run_success_returns_stdout(tmp_path):
    assert System(tmp_path)._run(["sh", "-c", "echo ok"]) == "ok\n"
