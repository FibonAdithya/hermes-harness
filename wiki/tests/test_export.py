import os

import pytest

from hermes_wiki import cli
from hermes_wiki.export import export_public
from wikifixture import build_wiki


def _src(tmp_path, **kw):
    return build_wiki(tmp_path / "src", **kw)


def _files(root):
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def test_export_excludes_private_and_unfrontmattered(tmp_path):
    src = _src(tmp_path)
    dest = tmp_path / "pub"
    n = export_public(src, dest, "h1", 1000)
    files = _files(dest.resolve())
    assert n == 3
    assert {"alpha.md", "beta.md", "nights/2026-09-25.md", "TAGS.md", "CONVENTIONS.md"} <= files
    assert "gamma.md" not in files and "delta.md" not in files
    assert not any(f.startswith("inbox/") or f.startswith(".git") for f in files)


def test_links_to_private_pages_become_text(tmp_path):
    src = _src(tmp_path)
    dest = tmp_path / "pub"
    export_public(src, dest, "h1", 1000)
    text = (dest / "alpha.md").read_text()
    assert "[[beta]]" in text
    assert "gamma" in text and "[[gamma" not in text


def test_link_variants_to_private_pages(tmp_path):
    src = _src(tmp_path, extra_pages={"epsilon": (
        "---\ntype: topic\ntags: [ops]\nupdated: 2026-10-01\n---\n"
        "See [[gamma#top]], [[delta|d]], [[beta#h]] and [[nope]].\n")})
    dest = tmp_path / "pub"
    export_public(src, dest, "h1", 1000)
    text = (dest / "epsilon.md").read_text()
    assert "See gamma, delta, [[beta#h]] and nope." in text


def test_index_lines_filtered(tmp_path):
    src = _src(tmp_path)
    (src / "index.md").write_text("- [[alpha]] — a\n- [[gamma]] — g\n## Pages\n")
    dest = tmp_path / "pub"
    export_public(src, dest, "h1", 1000)
    assert (dest / "index.md").read_text() == "- [[alpha]] — a\n## Pages\n"


def test_index_line_with_mixed_links_dropped(tmp_path):
    src = _src(tmp_path, index_lines=["- [[alpha]] and [[gamma]]"])
    dest = tmp_path / "pub"
    export_public(src, dest, "h1", 1000)
    idx = (dest / "index.md").read_text()
    assert "- [[alpha]] and" not in idx and "[[gamma" not in idx and "[[beta]]" in idx


def test_swap_keeps_recent_exports(tmp_path):
    src = _src(tmp_path)
    dest = tmp_path / "dest"
    export_public(src, dest, "a", 1000)
    export_public(src, dest, "b", 2000)
    assert dest.resolve() == tmp_path / ".dest.2000"
    assert (tmp_path / ".dest.1000").exists()
    os.utime(tmp_path / ".dest.1000", (1000, 1000))
    os.utime(tmp_path / ".dest.2000", (2000, 2000))
    export_public(src, dest, "c", 1000 + 86401)
    assert not (tmp_path / ".dest.1000").exists()
    assert (tmp_path / ".dest.2000").exists()
    assert dest.resolve() == tmp_path / f".dest.{1000 + 86401}"


def test_same_second_gets_suffix(tmp_path):
    src = _src(tmp_path)
    dest = tmp_path / "dest"
    export_public(src, dest, "a", 1000)
    export_public(src, dest, "b", 1000)
    assert dest.resolve() == tmp_path / ".dest.1000-1"
    assert (tmp_path / ".dest.1000").exists()


def test_unchanged_head_is_a_no_op(tmp_path):
    src = _src(tmp_path)
    dest = tmp_path / "dest"
    export_public(src, dest, "a", 1000)
    first = dest.resolve()
    assert export_public(src, dest, "a", 2000) == -1
    assert dest.resolve() == first
    assert not (tmp_path / ".dest.2000").exists()


def test_refuses_real_directory(tmp_path):
    src = _src(tmp_path)
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "keep.txt").write_text("x")
    with pytest.raises(RuntimeError):
        export_public(src, dest, "a", 1000)
    assert (dest / "keep.txt").read_text() == "x"


def test_cli_export_public(tmp_path, monkeypatch):
    import subprocess

    src = _src(tmp_path)
    subprocess.run(["git", "init", "-q", str(src)], check=True)
    subprocess.run(["git", "-C", str(src), "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-q", "--allow-empty", "-m", "c"], check=True)
    dest = tmp_path / "dest"
    rc = cli.main(["export-public", "--src", str(src), "--dest", str(dest),
                   "--config", str(tmp_path / "missing.json")])
    assert rc == 0
    assert (dest / "alpha.md").exists()
    assert (dest / ".source-head").read_text().strip() != ""


def test_retention_counts_from_retirement(tmp_path):
    src = _src(tmp_path)
    dest = tmp_path / "dest"
    export_public(src, dest, "h1", 1000)
    os.utime(tmp_path / ".dest.1000", (1000, 1000))
    export_public(src, dest, "h2", 1000 + 200000)
    assert (tmp_path / ".dest.1000").exists()
    export_public(src, dest, "h3", 1000 + 200000 + 86401)
    assert not (tmp_path / ".dest.1000").exists()
    assert (tmp_path / ".dest.201000").exists()


def test_cli_export_public_git_failure(tmp_path, capsys):
    src = _src(tmp_path)
    rc = cli.main(["export-public", "--src", str(src), "--dest", str(tmp_path / "d")])
    assert rc == 2
    err = capsys.readouterr().err
    assert "hermes-wiki" in err and "Traceback" not in err


from test_schema import LEAKY_FRONTMATTER  # noqa: E402


def test_leaky_tag_spellings_not_exported(tmp_path):
    extra = {f"leak-{n}": f"---\n{fm}---\nsecret-{n} body\n" for n, fm in LEAKY_FRONTMATTER.items()}
    extra["ok-list"] = "---\ntype: topic\ntags: [hermes, cost]\n---\nok list\n"
    extra["ok-str"] = '---\ntype: topic\ntags: "hermes"\n---\nok str\n'
    src = _src(tmp_path, extra_pages=extra)
    dest = tmp_path / "pub"
    export_public(src, dest, "h1", 1000)
    files = _files(dest.resolve())
    assert {"ok-list.md", "ok-str.md"} <= files
    assert not [f for f in files if f.startswith("leak-")]
    text = "".join((dest / f).read_text() for f in files)
    assert "secret-" not in text and "[[leak-" not in text


def test_filter_version_change_forces_rebuild(tmp_path, monkeypatch):
    from hermes_wiki import export as export_mod
    src = _src(tmp_path)
    dest = tmp_path / "dest"
    export_public(src, dest, "a", 1000)
    assert (dest / ".source-head").read_text().split() == ["a", export_mod.FILTER_VERSION]
    assert export_public(src, dest, "a", 1500) == -1
    monkeypatch.setattr(export_mod, "FILTER_VERSION", "filter-v999")
    assert export_public(src, dest, "a", 2000) == 3
    assert dest.resolve().name == ".dest.2000"


def test_export_made_before_filter_version_is_rebuilt(tmp_path):
    # An export written by the old code holds only the head; the same head must not skip the rebuild.
    src = _src(tmp_path)
    old = tmp_path / ".dest.500"
    old.mkdir()
    (old / ".source-head").write_text("a\n")
    (old / "leak.md").write_text("old filter output\n")
    os.symlink(old.name, tmp_path / "dest")
    assert export_public(src, tmp_path / "dest", "a", 1000) == 3
    assert not (tmp_path / "dest" / "leak.md").exists()
