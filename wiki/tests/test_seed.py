"""`hermes-wiki seed` against a fake `gh api` (never the real one)."""
import base64
import json
import subprocess
from pathlib import Path

from hermes_wiki import schema, seed

SEED_DIR = Path(__file__).resolve().parents[1] / "seed"
REPO = "FibonAdithya/hermes-wiki"


class FakeGh:
    """Simulates `gh api` GET and PUT on repos/<repo>/contents/<path>."""

    def __init__(self, files):
        self.files = dict(files)  # path -> text
        self.puts = []

    def __call__(self, argv, **kw):
        assert argv[:2] == ["gh", "api"], argv
        put = "-X" in argv and argv[argv.index("-X") + 1] == "PUT"
        endpoint = next(a for a in argv[2:] if a.startswith("repos/"))
        prefix = f"repos/{REPO}/contents/"
        assert endpoint.startswith(prefix), endpoint
        path = endpoint[len(prefix):]
        if put:
            fields = dict(argv[i + 1].split("=", 1) for i, a in enumerate(argv) if a == "-f")
            self.puts.append((path, fields))
            self.files[path] = base64.b64decode(fields["content"]).decode("utf-8")
            return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")
        if path not in self.files:
            return subprocess.CompletedProcess(argv, 1, stdout='{"message":"Not Found","status":"404"}',
                                               stderr="gh: Not Found (HTTP 404)")
        enc = base64.encodebytes(self.files[path].encode("utf-8")).decode("ascii")  # GitHub wraps at 60/76
        body = {"content": enc, "encoding": "base64", "sha": f"sha-{path}"}
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(body), stderr="")


def _before():
    return (SEED_DIR / "CONVENTIONS.before.md").read_text(encoding="utf-8")


def test_seed_creates_tags_and_replaces_unchanged_conventions():
    gh = FakeGh({"CONVENTIONS.md": _before()})
    actions = seed.seed(REPO, run=gh)
    assert actions == ["created TAGS.md", "replaced CONVENTIONS.md"]
    assert gh.files["TAGS.md"] == (SEED_DIR / "TAGS.md").read_text(encoding="utf-8")
    assert gh.files["CONVENTIONS.md"] == (SEED_DIR / "CONVENTIONS.md").read_text(encoding="utf-8")
    puts = dict(gh.puts)
    assert "sha" not in puts["TAGS.md"]
    assert puts["CONVENTIONS.md"]["sha"] == "sha-CONVENTIONS.md"
    assert puts["CONVENTIONS.md"]["branch"] == "main"


def test_seed_leaves_changed_conventions():
    gh = FakeGh({"CONVENTIONS.md": _before() + "- A rule Hermes added.\n", "TAGS.md": "- hermes — x\n"})
    actions = seed.seed(REPO, run=gh)
    assert gh.puts == []
    assert any("not replaced" in a for a in actions)
    assert "CONVENTIONS.md changed since planning; not replaced" in actions
    assert "TAGS.md exists; left alone" in actions


def test_seed_force_replaces_changed_conventions():
    gh = FakeGh({"CONVENTIONS.md": "something else\n", "TAGS.md": "- hermes — x\n"})
    actions = seed.seed(REPO, force=True, run=gh)
    assert [p for p, _ in gh.puts] == ["CONVENTIONS.md"]
    assert actions[-1] == "replaced CONVENTIONS.md"
    assert gh.files["TAGS.md"] == "- hermes — x\n"


def test_seed_get_error_other_than_404_raises():
    def broken(argv, **kw):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="HTTP 401: Bad credentials")

    try:
        seed.seed(REPO, run=broken)
    except seed.SeedError as e:
        assert "401" in str(e)
    else:
        raise AssertionError("expected SeedError")


def test_seed_tags_include_private_tags():
    tags = schema.load_tags(SEED_DIR)
    assert tags >= {"finance", "investing", "health", "people"}
    expected = {"hermes", "droplet", "box", "cost", "people", "investing", "tig", "talos", "fleet", "coding",
                "finance", "travel", "health"}
    assert tags == expected
    lines = [ln for ln in (SEED_DIR / "TAGS.md").read_text(encoding="utf-8").splitlines() if ln.startswith("- ")]
    assert len(lines) == 13 and all(" — " in ln for ln in lines)


REWORDED = '- Every page gets a one-line entry under "## Pages" in `index.md`.'


def _bullets(text):
    """Each `- ` bullet with its indented continuation lines, as one string."""
    out, cur = [], None
    for ln in text.splitlines():
        if ln.startswith("- "):
            cur = [ln]
            out.append(cur)
        elif cur is not None and ln.startswith("  "):
            cur.append(ln)
        else:
            cur = None
    return ["\n".join(b) for b in out]


def test_conventions_keeps_every_existing_rule():
    before = _bullets(_before())
    after = (SEED_DIR / "CONVENTIONS.md").read_text(encoding="utf-8")
    assert REWORDED in before, "the deliberately reworded rule is no longer in CONVENTIONS.before.md"
    assert len(before) == 9
    for bullet in before:
        if bullet != REWORDED:
            assert bullet in after, f"rule dropped or changed: {bullet!r}"
    assert "Every page gets a one-line entry in `index.md`, under the heading for its\n  type" in after
    for needle in ("only the curator edits pages from notes; never hand-edit `inbox/`",
                   r"rg -l '^tags:.*\bcost\b'", "1,500", "type: person", "[[wikilink]]"):
        assert needle in after


def _stub_gh(tmp_path, body):
    gh = tmp_path / "gh"
    (tmp_path / "body.json").write_text(json.dumps(body), encoding="utf-8")
    gh.write_text(f"#!/bin/sh\ncat '{tmp_path / 'body.json'}'\n", encoding="utf-8")
    gh.chmod(0o755)
    return str(gh)


def test_seed_undecodable_content_is_a_seed_error():
    for content in ("!!!notbase64", base64.b64encode(b"\xff\xfe").decode()):
        gh = FakeGh({"TAGS.md": "x"})
        gh.files["CONVENTIONS.md"] = None

        def run(argv, _gh=gh, _c=content, **kw):
            if argv[-1].endswith("CONVENTIONS.md") and "-X" not in argv:
                return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({"content": _c, "sha": "s"}), stderr="")
            return _gh(argv, **kw)

        try:
            seed.seed(REPO, run=run)
        except seed.SeedError as e:
            assert "CONVENTIONS.md" in str(e)
        else:
            raise AssertionError("expected SeedError")


def test_cli_seed_undecodable_exits_2(tmp_path, capsys):
    from hermes_wiki import cli

    gh = _stub_gh(tmp_path, {"content": base64.b64encode(b"\xff\xfe").decode(), "sha": "s"})
    assert cli.main(["seed", "--gh", gh, "--repo", REPO]) == 2
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1 and err[0].startswith("hermes-wiki: seed failed:")


def test_cli_seed_parser():
    from hermes_wiki import cli

    args = cli.build_parser().parse_args(["seed", "--force"])
    assert args.force is True and args.needs_config is False
