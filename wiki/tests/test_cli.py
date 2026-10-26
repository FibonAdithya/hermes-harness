import subprocess
import sys

from hermes_wiki import cli


def test_cli_core_imports_without_mcp():
    code = "import sys; sys.modules['mcp']=None; import hermes_wiki.cli, hermes_wiki.search, hermes_wiki.notes"
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_mcp_subcommand_dispatch(monkeypatch, tmp_path):
    from hermes_wiki import mcp_server
    from wikifixture import build_wiki
    import json

    root = build_wiki(tmp_path / "wiki")
    conf = tmp_path / "c.json"
    conf.write_text(json.dumps({"root": str(root), "source": "claude-laptop"}))
    seen = []
    monkeypatch.setattr(mcp_server, "serve", lambda cfg, write: seen.append((cfg.source, write)))
    assert cli.main(["mcp", "--config", str(conf)]) == 0
    assert cli.main(["mcp", "--config", str(conf), "--no-write"]) == 0
    assert seen == [("claude-laptop", True), ("claude-laptop", False)]
