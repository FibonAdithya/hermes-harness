import os

import pytest


@pytest.fixture(autouse=True)
def _no_real_gh_or_claude(tmp_path_factory, monkeypatch):
    """Put failing `gh` and `claude` stubs first on PATH so no test can reach the real binaries.

    A test that needs a fake injects its own (explicit path in the config, or its own PATH prepend)."""
    bindir = tmp_path_factory.mktemp("guard-bin")
    for name in ("gh", "claude"):
        stub = bindir / name
        stub.write_text(f"#!/bin/sh\necho '{name} disabled in tests' >&2\nexit 97\n", encoding="utf-8")
        stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:" + os.environ.get("PATH", ""))
    monkeypatch.delenv("HERMES_WIKI_DRY_PUBLISH", raising=False)
