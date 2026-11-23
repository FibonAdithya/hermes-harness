import dataclasses
import json
import os
import subprocess

import pytest
from wikifixture import build_wiki as _build_wiki

from hermes_wiki.reposcan import (
    HOSTS,
    SCAN_SH,
    Checkout,
    GhRepo,
    GitHubDown,
    Overrides,
    PageRef,
    Unreachable,
    load_overrides,
    one_repo,
    owner_repos,
    page_index,
    parse_slug,
    readme_line,
    scan_host,
)

TIG = HOSTS[1]
HERMES_VM = HOSTS[2]


def fake_run(stdout="", code=0, raises=None):
    def run(argv, **kwargs):
        run.calls.append(argv)
        run.kwargs.append(kwargs)
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(argv, code, stdout, "")

    run.calls = []
    run.kwargs = []
    return run


def test_parse_slug_strips_credentials_and_suffix():
    assert parse_slug("https://x-access-token:SECRET@github.com/o/r.git") == "o/r"
    assert parse_slug("git@github.com:FibonAdithya/chain.git") == "FibonAdithya/chain"
    assert parse_slug("https://gitlab.com/o/r.git") is None
    assert parse_slug("/srv/git/r") is None
    assert parse_slug("") is None


def test_scan_git_mode_parses_rows():
    out = (
        "root\t/home/adi\n"
        "repo\t/home/adi/TIG/talos\thttps://github.com/FibonAdithya/talos.git\t\t\n"
        "repo\t/home/adi/MIP_gen\t\t12\t2026-11-17\n"
        "repo\t/home/adi/echo_ops\t\t0\t2026-06-01\n"
        "end\t3\n"
    )
    cs = scan_host(TIG, fake_run(stdout=out), "2026-10-24")
    assert cs == [
        Checkout("tig-adi", "/home/adi/TIG/talos", "FibonAdithya/talos", "talos", None, None),
        Checkout("tig-adi", "/home/adi/MIP_gen", None, "MIP_gen", 12, "2026-11-17"),
        Checkout("tig-adi", "/home/adi/echo_ops", None, "echo_ops", 0, "2026-06-01"),
    ]


def test_github_origin_ignores_counts_and_gitlab_origin_keeps_them():
    out = (
        "root\t/h\n"
        "repo\t/h/a\thttps://github.com/o/a.git\t5\t2026-11-01\n"
        "repo\t/h/b\thttps://gitlab.com/o/b.git\t3\t2026-11-02\n"
        "end\t2\n"
    )
    cs = scan_host(TIG, fake_run(stdout=out), "2026-10-24")
    assert cs[0].slug == "o/a" and cs[0].commits_30d is None and cs[0].last_commit is None
    assert cs[1].slug is None and cs[1].commits_30d == 3 and cs[1].last_commit == "2026-11-02"


def test_banner_without_header_is_unreachable():
    with pytest.raises(Unreachable):
        scan_host(TIG, fake_run(stdout="Welcome to Ubuntu\n"), "2026-10-24")


def test_truncated_after_header_is_unreachable():
    out = "root\t/h\nrepo\t/h/a\t\t1\t2026-11-01\n"
    with pytest.raises(Unreachable):
        scan_host(TIG, fake_run(stdout=out), "2026-10-24")


def test_end_count_mismatch_is_unreachable():
    out = "root\t/h\nrepo\t/h/a\t\t1\t2026-11-01\nend\t2\n"
    with pytest.raises(Unreachable):
        scan_host(TIG, fake_run(stdout=out), "2026-10-24")


def test_empty_output_is_unreachable():
    with pytest.raises(Unreachable):
        scan_host(TIG, fake_run(stdout=""), "2026-10-24")


def test_header_after_banner_is_accepted():
    out = "Welcome to Ubuntu\nroot\t/h\nrepo\t/h/x\t\t1\t2026-11-01\nend\t1\n"
    cs = scan_host(TIG, fake_run(stdout=out), "2026-10-24")
    assert [c.local_name for c in cs] == ["x"]


def test_nonzero_exit_timeout_and_oserror_are_unreachable():
    for run in (
        fake_run(code=255),
        fake_run(raises=subprocess.TimeoutExpired("ssh", 20)),
        fake_run(raises=FileNotFoundError("ssh")),
    ):
        with pytest.raises(Unreachable):
            scan_host(TIG, run, "2026-10-24")


def test_names_mode_renders_display_root():
    out = "root\t/home/hermes/.hermes/sandboxes/docker/default/home/repos\nname\tchain\nend\t1\n"
    cs = scan_host(HERMES_VM, fake_run(stdout=out), "2026-10-24")
    assert cs == [Checkout("hermes-vm", "/root/repos/chain", None, "chain", None, None)]


def test_malformed_lines_are_skipped():
    out = "root\t/h\nrepo\tonly-two-fields\ngarbage\nend\t1\n"
    assert scan_host(TIG, fake_run(stdout=out), "2026-10-24") == []


def test_ssh_argv_and_timeout():
    run = fake_run(stdout="root\t/h\nend\t0\n")
    scan_host(TIG, run, "2026-10-24")
    assert run.calls[0] == [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "tig-adi",
        "bash", "-s", "--", "''", "3", "git", "2026-10-24",
    ]
    assert run.kwargs[0]["timeout"] == 20
    assert run.kwargs[0]["input"] == SCAN_SH


def test_ssh_args_with_spaces_are_quoted():
    run = fake_run(stdout="root\t/h\nend\t0\n")
    scan_host(dataclasses.replace(TIG, root="my repos"), run, "2026-10-24")
    assert run.calls[0][9] == "'my repos'"


def test_local_host_runs_bash_directly():
    run = fake_run(stdout="root\t/h\nend\t0\n")
    scan_host(HOSTS[0], run, "2026-10-24")
    assert run.calls[0] == ["bash", "-s", "--", "", "3", "git", "2026-10-24"]
    assert run.kwargs[0]["input"] == SCAN_SH


def _git(cwd, *args, env):
    subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True)


def test_scan_sh_on_a_real_tree(tmp_path):
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    a = tmp_path / "proj" / "a"
    b = tmp_path / "b"
    pruned = tmp_path / ".worktrees" / "w"
    for d in (a, b, pruned):
        d.mkdir(parents=True)
        _git(d, "init", "-q", "-b", "main", env=env)
        _git(d, "commit", "-q", "--allow-empty", "-m", "x", env=env)
    unborn = tmp_path / "unborn"
    unborn.mkdir()
    detached = tmp_path / "detached"
    detached.mkdir()
    _git(detached, "init", "-q", "-b", "main", env=env)
    _git(detached, "commit", "-q", "--allow-empty", "-m", "x", env=env)
    _git(detached, "checkout", "-q", "--detach", env=env)
    _git(detached, "branch", "-q", "-D", "main", env=env)
    _git(b, "worktree", "add", "-q", str(tmp_path / "b-wt"), "-b", "wt", env=env)
    _git(unborn, "init", "-q", env=env)
    _git(a, "remote", "add", "origin", "https://github.com/o/a.git", env=env)
    r = subprocess.run(
        ["bash", "-s", "--", "", "3", "git", "2000-01-01"],
        input=SCAN_SH, text=True, capture_output=True, env=env, check=True,
    )
    lines = r.stdout.splitlines()
    assert lines[0] == f"root\t{tmp_path}"
    rows = {r.split("\t")[1]: r.split("\t") for r in lines[1:-1]}
    assert len(rows) == 4
    assert lines[-1] == "end\t4"
    # the shell computes counts for every repo; scan_host discards them for GitHub origins
    assert rows[str(a)][:4] == ["repo", str(a), "https://github.com/o/a.git", "1"]
    assert rows[str(b)][:4] == ["repo", str(b), "", "1"] and len(rows[str(b)][4]) == 10
    assert rows[str(unborn)] == ["repo", str(unborn), "", "0", ""]
    cs = scan_host(HOSTS[0], lambda argv, **kw: subprocess.CompletedProcess(argv, 0, r.stdout, ""), "2000-01-01")
    assert {c.local_name: (c.slug, c.commits_30d) for c in cs} == {"a": ("o/a", None), "b": (None, 1), "unborn": (None, 0), "detached": (None, 1)}
    assert [c.last_commit for c in cs if c.local_name == "detached"][0] is not None
    assert [c.last_commit for c in cs if c.local_name == "unborn"] == [None]


def test_scan_sh_names_mode_and_missing_root(tmp_path):
    env = {**os.environ, "HOME": str(tmp_path)}
    (tmp_path / "r" / "chain").mkdir(parents=True)
    ok = subprocess.run(["bash", "-s", "--", "r", "1", "names", "2000-01-01"],
                        input=SCAN_SH, text=True, capture_output=True, env=env)
    assert ok.stdout == f"root\t{tmp_path}/r\nname\tchain\nend\t1\n"
    bad = subprocess.run(["bash", "-s", "--", "nope", "1", "names", "2000-01-01"],
                         input=SCAN_SH, text=True, capture_output=True, env=env)
    assert bad.returncode == 3


NODE = {"nameWithOwner": "FibonAdithya/chain", "description": "d", "isFork": False, "isArchived": False,
        "defaultBranchRef": {"target": {"committedDate": "2026-11-17T10:00:00Z",
                                        "history": {"totalCount": 37}}}}


def test_owner_repos_parses_nodes_and_sends_since():
    run = fake_run(stdout=json.dumps(NODE) + "\n" + json.dumps(
        {**NODE, "nameWithOwner": "FibonAdithya/empty", "defaultBranchRef": None}) + "\n")
    got = owner_repos("FibonAdithya", run, "2026-10-24T09:00:00Z")
    assert got[0] == GhRepo("FibonAdithya/chain", "d", False, False, 37, "2026-11-17")
    assert got[1].commits_30d == 0 and got[1].last_commit is None
    assert "since=2026-10-24T09:00:00Z" in run.calls[0] and "owner=FibonAdithya" in run.calls[0]
    assert run.kwargs[0]["timeout"] == 60


def test_owner_repos_null_description_is_empty_string():
    run = fake_run(stdout=json.dumps({**NODE, "description": None}) + "\n")
    assert owner_repos("FibonAdithya", run, "2026-10-24T09:00:00Z")[0].description == ""


def test_owner_repos_zero_repos_is_down():
    with pytest.raises(GitHubDown):
        owner_repos("FibonAdithya", fake_run(stdout=""), "2026-10-24T09:00:00Z")


def test_owner_repos_failure_is_down():
    with pytest.raises(GitHubDown):
        owner_repos("FibonAdithya", fake_run(code=1), "2026-10-24T09:00:00Z")


def test_owner_repos_timeout_and_bad_line_are_down():
    with pytest.raises(GitHubDown):
        owner_repos("o", fake_run(raises=subprocess.TimeoutExpired("gh", 60)), "2026-10-24T09:00:00Z")
    with pytest.raises(GitHubDown):
        owner_repos("o", fake_run(stdout="not json\n"), "2026-10-24T09:00:00Z")


def test_one_repo_parses_and_sends_owner_name_since():
    run = fake_run(stdout=json.dumps(NODE) + "\n")
    got = one_repo("FibonAdithya/chain", run, "2026-10-24T09:00:00Z")
    assert got == GhRepo("FibonAdithya/chain", "d", False, False, 37, "2026-11-17")
    argv = run.calls[0]
    assert "owner=FibonAdithya" in argv and "name=chain" in argv and "since=2026-10-24T09:00:00Z" in argv


def test_one_repo_failure_is_none():
    assert one_repo("tig-foundation/gone", fake_run(code=1), "2026-10-24T09:00:00Z") is None
    assert one_repo("tig-foundation/gone", fake_run(stdout=""), "2026-10-24T09:00:00Z") is None
    assert one_repo("noslash", fake_run(stdout=json.dumps(NODE)), "2026-10-24T09:00:00Z") is None


def test_owner_repos_lists_only_owned_repos():
    run = fake_run(stdout=json.dumps(NODE) + "\n" + json.dumps({**NODE, "nameWithOwner": "someone/else"}) + "\n")
    got = owner_repos("fibonadithya", run, "2026-10-24T09:00:00Z")
    assert [g.slug for g in got] == ["FibonAdithya/chain"]
    assert "ownerAffiliations:[OWNER]" in " ".join(run.calls[0])


TAGS = {"coding": "c", "finance": "f"}
# the fixture's base page "delta" has no frontmatter, which page_index rejects; replace it
DELTA = "---\ntype: project\ntags: [coding]\n---\nDelta.\n"


def build_wiki(root, extra_pages=None, **kw):
    return _build_wiki(root, extra_pages={"delta": DELTA, **(extra_pages or {})}, **kw)


def fm(repos, tags="[coding]"):
    return f"---\ntype: project\ntags: {tags}\nrepos: {repos}\n---\n"


def test_page_index_any_private_page_marks_private(tmp_path):
    root = build_wiki(tmp_path, extra_tags=TAGS, extra_pages={
        "fleet": fm("[fleet, valuebook]") + "# Fleet\n\nSelf-healing agents. More.\n",
        "valuebook": fm("[valuebook]", "[finance, coding]") + "# FV\n\nValuation.\n"})
    idx = page_index(root)
    assert {p.name: p.private for p in idx["valuebook"]} == {"fleet": False, "valuebook": True}
    assert idx["fleet"][0].purpose == "Self-healing agents."
    assert [p.name for p in idx["valuebook"]] == ["fleet", "valuebook"]  # sorted by page name
    assert isinstance(idx["fleet"][0], PageRef)


def test_page_index_keys_lowercase_and_scalar_repos(tmp_path):
    root = build_wiki(tmp_path, extra_tags=TAGS, extra_pages={
        "a": fm("[Chain]") + "Alpha.\n",
        "b": "---\ntype: project\ntags: [coding]\nrepos: chain, Other\n---\nBeta.\n",
        "c": "---\ntype: project\ntags: [coding]\n---\nGamma.\n"})
    idx = page_index(root)
    assert [p.name for p in idx["chain"]] == ["a", "b"]
    assert [p.name for p in idx["other"]] == ["b"]
    assert "Chain" not in idx


def test_scalar_repos_on_private_page_marks_private(tmp_path):
    root = build_wiki(tmp_path, extra_tags=TAGS, extra_pages={
        "secret": "---\ntype: project\ntags: [finance]\nrepos: valuebook\n---\nHidden.\n",
        "pub": fm("[valuebook]") + "Public.\n"})
    assert any(p.private for p in page_index(root)["valuebook"])


@pytest.mark.parametrize("repos", ["[valuebook, CoinJar", "[valuebook CoinJar]"])
def test_malformed_repos_entry_raises_naming_page(tmp_path, repos):
    # a missing `]` or a missing comma must not yield a key that misses the real repo name
    root = build_wiki(tmp_path, extra_tags=TAGS, extra_pages={
        "money": fm(repos, "[finance]") + "Hidden.\n"})
    with pytest.raises(OSError, match=r"money: unparseable repos entry"):
        page_index(root)


def test_well_formed_repos_list_and_scalar_still_index(tmp_path):
    root = build_wiki(tmp_path, extra_tags=TAGS, extra_pages={
        "a": fm("[valuebook, FibonAdithya/Coin.Jar_2-x]", "[finance]") + "A.\n",
        "b": "---\ntype: project\ntags: [coding]\nrepos: chain\n---\nB.\n"})
    idx = page_index(root)
    assert set(idx) == {"valuebook", "fibonadithya/coin.jar_2-x", "chain"}


def test_unparsed_frontmatter_page_raises(tmp_path):
    page_index(build_wiki(tmp_path / "ok", extra_tags=TAGS, extra_pages={"p": fm("[r]") + "x.\n"}))
    root = build_wiki(tmp_path / "bad", extra_tags=TAGS, extra_pages={
        "broken": "---\ntype: project\ntags: [coding]\nrepos: [r]\nNo end\n"})
    with pytest.raises(OSError, match="broken"):
        page_index(root)


def test_purpose_heading_ends_paragraph(tmp_path):
    root = build_wiki(tmp_path, extra_tags=TAGS, extra_pages={
        "p": fm("[r]") + "Intro words\n## Next\nmore.\n"})
    assert page_index(root)["r"][0].purpose == "Intro words"


def test_readme_line_skips_multiline_comment(tmp_path):
    (tmp_path / "README.md").write_text("<!-- a\nhidden text\n-->\nReal line.\n")
    assert readme_line(str(tmp_path)) == "Real line."


def test_purpose_skips_comment_and_truncates(tmp_path):
    body = "# T\n\n<!-- x -->\n\n" + "word " * 60 + ".\n"
    root = build_wiki(tmp_path, extra_tags=TAGS, extra_pages={"p": fm("[r]") + body})
    p = page_index(root)["r"][0].purpose
    assert len(p) == 160 and p.endswith("…") and "<!--" not in p


def test_purpose_multiline_comment_and_paragraph_collapse(tmp_path):
    body = "# T\n\n<!-- a\nb -->\n\nFirst line\ncontinues here! Second.\n"
    root = build_wiki(tmp_path, extra_tags=TAGS, extra_pages={"p": fm("[r]") + body})
    assert page_index(root)["r"][0].purpose == "First line continues here!"


def test_page_index_unreadable_root_raises(tmp_path):
    with pytest.raises(OSError):
        page_index(tmp_path / "missing")
    (tmp_path / "empty").mkdir()
    with pytest.raises(OSError):
        page_index(tmp_path / "empty")


def test_readme_line_skips_heading_and_badge(tmp_path):
    (tmp_path / "README.md").write_text("# Title\n\n[![ci](x)](y)\nDoes the thing.\n")
    assert readme_line(str(tmp_path)) == "Does the thing."
    assert readme_line(str(tmp_path / "none")) is None


def test_readme_line_rst_comment_and_cap(tmp_path):
    (tmp_path / "README.rst").write_text("Title\n=====\n<!-- c -->\n" + "x" * 200 + "\n")
    r = readme_line(str(tmp_path))
    assert len(r) == 160 and r.endswith("…")
    (tmp_path / "e").mkdir()
    (tmp_path / "e" / "README").write_text("\n\n")
    assert readme_line(str(tmp_path / "e")) is None


def test_overrides_defaults_and_bad_type(tmp_path):
    assert load_overrides(tmp_path / "none.toml") == Overrides()
    assert Overrides().private == frozenset()
    (tmp_path / "b.toml").write_text('private = "local:MoneyNotes"\n')
    with pytest.raises(ValueError):
        load_overrides(tmp_path / "b.toml")


def test_overrides_private_is_the_file_list(tmp_path):
    f = tmp_path / "o.toml"
    f.write_text('private = ["FibonAdithya/x", "local:MoneyNotes"]\n')
    assert load_overrides(f).private == frozenset({"FibonAdithya/x", "local:MoneyNotes"})
    f.write_text("private = []\n")
    assert load_overrides(f).private == frozenset()


def test_overrides_parse_and_reject(tmp_path):
    f = tmp_path / "o.toml"
    f.write_text('owners = ["a"]\nprivate = ["x/y"]\n[purpose]\n"x/y" = "does z"\n')
    o = load_overrides(f)
    assert o.owners == ("a",) and o.private == frozenset({"x/y"}) and o.purpose == {"x/y": "does z"}
    assert o.clone_owners == Overrides().clone_owners
    for bad in ('ownerz = ["a"]\n', "owners = [1]\n", "[purpose]\nx = 1\n", "owners = [\n"):
        f.write_text(bad)
        with pytest.raises(ValueError):
            load_overrides(f)


@pytest.mark.parametrize("abbr", ["e.g.", "i.e.", "vs.", "cf.", "E.g."])
def test_purpose_does_not_end_at_an_abbreviation(tmp_path, abbr):
    body = f"Tools for agents, {abbr} Claude and Codex. Second sentence.\n"
    root = build_wiki(tmp_path, extra_tags=TAGS, extra_pages={"p": fm("[r]") + body})
    assert page_index(root)["r"][0].purpose == f"Tools for agents, {abbr} Claude and Codex."
