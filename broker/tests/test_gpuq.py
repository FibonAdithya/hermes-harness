import pytest
from hermes_broker.gpuq import (
    GPUQ_BIN,
    build_cancel_argv,
    build_list_argv,
    build_show_argv,
    build_submit_argv,
)


def test_submit_uses_absolute_path():
    """gpuq is not on the PATH for a non-interactive SSH shell."""
    argv = build_submit_argv(
        project="wgan-synthetic",
        commit="abc123",
        branch="hermes/eval",
        lane="gpu",
        artifacts=["runs/v0/summary.json"],
        command=["python", "-m", "src.train"],
    )
    assert argv[0] == GPUQ_BIN
    assert GPUQ_BIN.startswith("/")


def test_submit_shape():
    argv = build_submit_argv(
        project="wgan-synthetic",
        commit="abc123",
        branch="hermes/eval",
        lane="gpu",
        artifacts=["runs/v0/summary.json", "runs/v0/loss.png"],
        command=["python", "-m", "src.train", "--config", "configs/v0.yaml"],
    )
    assert argv[1] == "submit"
    assert "--project" in argv and argv[argv.index("--project") + 1] == "wgan-synthetic"
    assert "--commit" in argv and argv[argv.index("--commit") + 1] == "abc123"
    assert "--lane" in argv and argv[argv.index("--lane") + 1] == "gpu"
    assert argv.count("--artifact") == 2
    assert argv[-5:] == ["python", "-m", "src.train", "--config", "configs/v0.yaml"]
    assert argv[argv.index("--") + 1] == "python"


def test_commit_is_required():
    with pytest.raises(ValueError):
        build_submit_argv("p", "", "b", "gpu", [], ["python"])


def test_lane_is_validated():
    with pytest.raises(ValueError):
        build_submit_argv("p", "abc", "b", "tpu", [], ["python"])


def test_empty_command_rejected():
    with pytest.raises(ValueError):
        build_submit_argv("p", "abc", "b", "cpu", [], [])


def test_read_only_argvs():
    assert build_show_argv("j1") == [GPUQ_BIN, "show", "j1"]
    assert build_list_argv() == [GPUQ_BIN, "list"]
    assert build_cancel_argv("j1") == [GPUQ_BIN, "cancel", "j1"]


def test_job_ids_are_validated_before_reaching_a_remote_shell():
    """tig-gpu has no forced command: argv is joined and run by the remote login
    shell, so a job id with shell metacharacters is a remote shell."""
    for bad in ["x; id", "$(id)", "a b", "", "x|sh", "../y", "x" * 65]:
        with pytest.raises(ValueError):
            build_show_argv(bad)
        with pytest.raises(ValueError):
            build_cancel_argv(bad)
    assert build_show_argv("job-12.3_x")[-1] == "job-12.3_x"


def test_logs_argv_has_no_shell(monkeypatch):
    from hermes_broker.gpuq import build_logs_argv

    argv = build_logs_argv("job-1", 50)
    assert "sh" not in argv and not any("|" in a for a in argv)
    assert argv == [GPUQ_BIN, "show", "job-1"]
    with pytest.raises(ValueError):
        build_logs_argv("x; id", 50)
