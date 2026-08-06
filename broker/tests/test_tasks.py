import pytest
from hermes_broker.tasks import (
    build_launch_argv,
    build_log_argv,
    build_status_argv,
    build_write_prompt_argv,
    new_task_id,
)


def test_task_id_is_slug_safe():
    task_id = new_task_id(now=1754400000.0)
    assert task_id.replace("-", "").isalnum()
    assert "/" not in task_id and " " not in task_id


def test_task_ids_differ():
    assert new_task_id(now=1754400000.0) != new_task_id(now=1754400000.0)


def test_launch_argv_shape():
    argv = build_launch_argv("t-abc", "fibonadithya/notes-tools", "/srv/hermes-tasks/t-abc/prompt.txt")
    assert argv[0] == "/opt/hermes-exec/run-task.sh"
    assert argv[1:] == ["t-abc", "fibonadithya/notes-tools", "/srv/hermes-tasks/t-abc/prompt.txt"]


def test_prompt_is_written_from_stdin_not_argv():
    """The prompt is attacker-influenced text; it must never reach a command line."""
    argv = build_write_prompt_argv("t-abc")
    joined = " ".join(argv)
    assert "cat" in joined
    assert "/srv/hermes-tasks/t-abc/prompt.txt" in joined


def test_status_and_log_argvs():
    assert build_status_argv("t-abc")[-1].endswith("t-abc/status")
    argv = build_log_argv("t-abc", lines=50)
    assert "50" in argv
    assert argv[-1].endswith("t-abc/log")


def test_task_id_is_validated():
    for bad in ["../etc", "a b", "a/b", ""]:
        with pytest.raises(ValueError):
            build_status_argv(bad)
