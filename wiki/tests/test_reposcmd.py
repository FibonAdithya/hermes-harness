import io
import json
import os
import shutil
import subprocess
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from wikifixture import build_wiki

from hermes_wiki import cli, todos
from hermes_wiki.config import WikiConfig
from hermes_wiki.reposcmd import run_repos
from hermes_wiki.secretscan import find_secrets

TAGS = {"coding": "c", "finance": "f"}
# the base fixture page "delta" has no frontmatter, which page_index rejects; replace it
DELTA = "---\ntype: project\ntags: [coding]\n---\nDelta.\n"
PAGES = {
    "delta": DELTA,
    "chain": "---\ntype: project\ntags: [coding]\nrepos: [chain]\n---\nChain runs headless sessions.\n",
    "valuebook": "---\ntype: project\ntags: [finance, coding]\nrepos: [valuebook]\n---\nValuation.\n",
}
SECRET = "api_key=abcd1234!"


def node(slug, desc, commits, last, fork=False):
    return {"nameWithOwner": slug, "description": desc, "isFork": fork, "isArchived": False,
            "defaultBranchRef": {"target": {"committedDate": f"{last}T10:00:00Z",
                                            "history": {"totalCount": commits}}}}


class Result:
    def __init__(self, stdout="", code=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, code, stderr


class FakeRun:
    """Answers git pull, gh graphql (owner listing and one_repo) and the three host scans."""

    def __init__(self, root):
        self.root = str(root)
        self.calls = []
        self.pull_code = 0
        self.github = "ok"  # ok | down | empty
        self.owned = {
            "FibonAdithya/chain": node("FibonAdithya/chain", "Headless pipeline.", 37, "2026-11-20"),
            "FibonAdithya/valuebook": node("FibonAdithya/valuebook", "Valuation model.", 5, "2026-11-10"),
            "FibonAdithya/fleet": node("FibonAdithya/fleet", "Agent fleet.", 0, "2026-08-01"),
            "FibonAdithya/oldfork": node("FibonAdithya/oldfork", "A fork.", 0, "2026-01-01", fork=True),
        }
        self.others = {
            "tig-foundation/talos": node("tig-foundation/talos", "Talos solver.", 3, "2026-11-15"),
            "tig-foundation/tig-knowledge-base": node("tig-foundation/tig-knowledge-base", "TIG notes.",
                                                      1, "2026-11-02"),
        }
        self.failed_lookups = set()
        self.down_hosts = set()
        self.laptop = [
            ("/home/u/code/chain", "git@github.com:FibonAdithya/chain.git", "", ""),
            ("/home/u/code/valuebook", "https://github.com/FibonAdithya/valuebook.git", "", ""),
            ("/home/u/scratch/toy", "", "2", "2026-11-01"),
        ]
        self.tig_adi = [
            ("/home/adi/TIG/talos", "https://github.com/tig-foundation/talos.git", "", ""),
            ("/home/adi/TIG/tig-knowledge-base", "git@github.com:tig-foundation/tig-knowledge-base.git",
             "", ""),
        ]
        self.mirrors = ["chain", "fleet"]

    @staticmethod
    def _git_scan(root, rows):
        lines = [f"root\t{root}"] + ["repo\t" + "\t".join(r) for r in rows] + [f"end\t{len(rows)}"]
        return "\n".join(lines) + "\n"

    def lookups(self):
        return [c for c in self.calls if c[:3] == ["gh", "api", "graphql"] and "--paginate" not in c]

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append(argv)
        if argv[:2] == ["git", "-C"] and argv[3:] == ["pull", "--ff-only", "-q"]:
            assert argv[2] == self.root
            if self.pull_code == "timeout":
                raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))
            return Result(code=self.pull_code, stderr="" if self.pull_code == 0 else "fatal: offline")
        if argv[:3] == ["gh", "api", "graphql"]:
            fields = dict(a.split("=", 1) for a in argv if "=" in a and not a.startswith("query="))
            if "--paginate" in argv:
                if self.github == "down":
                    return Result(code=1, stderr="HTTP 401")
                if self.github == "empty":
                    return Result("")
                assert fields["owner"] == "FibonAdithya"
                return Result("".join(json.dumps(n) + "\n" for n in self.owned.values()))
            slug = f"{fields['owner']}/{fields['name']}"
            hit = {k.lower(): v for k, v in {**self.others, **self.owned}.items()}.get(slug.lower())
            if hit is None or slug.lower() in self.failed_lookups:
                return Result(code=1, stderr="Could not resolve to a Repository")
            return Result(json.dumps(hit))
        if argv[0] == "bash":
            assert kwargs.get("input"), "the scan script goes on stdin"
            return Result(self._git_scan("/home/u", self.laptop))
        if argv[0] == "ssh":
            host = argv[5]
            if host in self.down_hosts:
                return Result(code=255, stderr="ssh: connect to host: timed out")
            if host == "tig-adi":
                return Result(self._git_scan("/home/adi", self.tig_adi))
            if host == "hermes-vm":
                root = "/home/hermes/.hermes/sandboxes/docker/default/home/repos"
                lines = [f"root\t{root}"] + [f"name\t{n}" for n in self.mirrors] + [f"end\t{len(self.mirrors)}"]
                return Result("\n".join(lines) + "\n")
        raise AssertionError(f"unexpected command: {argv}")


class RecordingStore:
    def __init__(self, inner, puts, fail_on=()):
        self.inner, self.puts, self.fail_on = inner, puts, set(fail_on)

    def get(self, path):
        return self.inner.get(path)

    def put(self, path, text, sha, message):
        assert message == "repos: refresh"
        if path in self.fail_on:
            raise todos.StoreError("HTTP 500")
        self.puts.append(path)
        self.inner.put(path, text, sha, message)


class Env:
    def __init__(self, tmp_path):
        self.root = build_wiki(tmp_path / "wiki", extra_pages=PAGES, extra_tags=TAGS)
        self.state = tmp_path / "state"
        self.cfg = WikiConfig(root=self.root, source="test", pull=True, state_dir=self.state)
        self.overrides = tmp_path / "repos.toml"
        self.overrides.write_text("private = []\n")
        self.fake = FakeRun(self.root)
        self.dry = todos.DryContents(tmp_path / "dry")
        self.puts = []
        self.store = RecordingStore(self.dry, self.puts)
        self.now = datetime(2026, 11, 24, 9, 0, tzinfo=timezone.utc)
        self._out = io.StringIO()
        self._err = io.StringIO()

    def run_repos(self, dry_run=False):
        self._out, self._err = io.StringIO(), io.StringIO()
        return run_repos(self.cfg, run=self.fake, store=self.store, now=self.now,
                         overrides_path=self.overrides, dry_run=dry_run, out=self._out, err=self._err,
                         sleep=lambda s: None)

    def out(self):
        return self._out.getvalue()

    def err(self):
        return self._err.getvalue()

    def page(self, path):
        got = self.dry.get(path)
        assert got is not None, f"{path} was never written"
        return got[0]

    def set_commits(self, slug, n):
        self.fake.owned[slug]["defaultBranchRef"]["target"]["history"]["totalCount"] = n

    def set_last(self, slug, day):
        self.fake.owned[slug]["defaultBranchRef"]["target"]["committedDate"] = f"{day}T10:00:00Z"

    def fail_ssh(self, host):
        self.fake.down_hosts.add(host)

    def fail_lookup(self, slug):
        self.fake.failed_lookups.add(slug.lower())

    def break_(self, how):
        if how == "pull_fails":
            self.fake.pull_code = 1
        elif how == "github_down":
            self.fake.github = "down"
        elif how == "github_empty":
            self.fake.github = "empty"
        elif how == "no_wiki":
            shutil.rmtree(self.root)
        elif how == "secret":
            self.fake.owned["FibonAdithya/fleet"]["description"] = SECRET
            assert find_secrets(SECRET), "the planted string must be one the scan flags"
        elif how == "private_leak":
            # Every text source of the public chain row names the private repo. The purpose
            # sources are scrubbed by build_rows, but a checkout path is not, so only the final
            # check can stop it.
            self.fake.owned["FibonAdithya/chain"]["description"] = "Feeds valuebook."
            self.overrides.write_text('[purpose]\n"FibonAdithya/chain" = "Feeds valuebook."\n')
            self.fake.laptop[0] = ("/home/u/valuebook/chain", "git@github.com:FibonAdithya/chain.git", "", "")
        else:
            raise AssertionError(how)


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def test_full_run_writes_both_pages_and_state(env):
    assert env.run_repos() == 0, env.err()
    pub, priv = env.page("repos.md"), env.page("repos-private.md")
    assert "FibonAdithya/chain" in pub and "valuebook" not in pub.lower()
    assert "FibonAdithya/valuebook" in priv
    assert json.loads((env.state / "repos-state.json").read_text())["hosts"]["tig-adi"]
    assert env.puts == ["repos.md", "repos-private.md"]
    assert "repos: wrote 2 page(s)" in env.out()


def test_full_run_page_contents(env):
    env.run_repos()
    pub = env.page("repos.md")
    assert "updated: 2026-11-24" in pub
    assert "/root/repos/chain" in pub and "/root/repos/fleet" in pub
    assert "/home/adi/TIG/talos" in pub and "tig-foundation/talos" in pub
    assert "oldfork" not in pub  # a fork is not listed
    assert "toy (local only)" in pub
    assert "Chain runs headless sessions." in pub
    assert "37" not in pub  # the raw count is never rendered


def test_state_holds_checkouts_by_host(env):
    env.run_repos()
    state = json.loads((env.state / "repos-state.json").read_text())
    assert set(state["hosts"]) == {"laptop", "tig-adi", "hermes-vm"}
    tig = state["hosts"]["tig-adi"]
    assert tig["date"] == "2026-11-24"
    assert {c["path"] for c in tig["checkouts"]} == {"/home/adi/TIG/talos", "/home/adi/TIG/tig-knowledge-base"}
    assert not list(env.state.glob("*.tmp*")), "the temp file is renamed away"


def test_second_run_unchanged_makes_no_write(env):
    env.run_repos(); n = len(env.puts)
    env.now += timedelta(days=1)
    assert env.run_repos() == 0 and len(env.puts) == n and "unchanged" in env.out()


def test_count_drop_within_band_no_write_new_commit_writes(env):
    env.run_repos(); n = len(env.puts)
    env.set_commits("FibonAdithya/chain", 30)           # was 37, still hot
    env.run_repos(); assert len(env.puts) == n
    env.set_last("FibonAdithya/chain", "2026-11-24")
    env.run_repos(); assert env.puts[n:] == ["repos.md"]
    assert "repos: wrote 1 page(s)" in env.out()


def test_tig_adi_down_uses_saved_paths(env):
    env.run_repos(); env.fail_ssh("tig-adi")
    env.now += timedelta(days=1)
    assert env.run_repos() == 0
    assert "tig-adi paths as of 2026-11-24 (unreachable today)" in env.page("repos.md")
    assert "/home/adi/TIG/talos" in env.page("repos.md")
    state = json.loads((env.state / "repos-state.json").read_text())
    assert state["hosts"]["tig-adi"]["date"] == "2026-11-24"  # kept, not replaced
    assert state["hosts"]["laptop"]["date"] == "2026-11-25"   # a successful scan replaces


def test_tig_adi_down_no_state_says_unknown(env):
    env.fail_ssh("tig-adi"); env.run_repos()
    assert "tig-adi paths unknown (unreachable today)" in env.page("repos.md")
    assert "/home/adi/TIG/talos" not in env.page("repos.md")


def test_corrupt_state_is_no_state(env):
    env.state.mkdir(parents=True)
    (env.state / "repos-state.json").write_text("{not json")
    env.fail_ssh("tig-adi")
    assert env.run_repos() == 0, env.err()
    assert "tig-adi paths unknown (unreachable today)" in env.page("repos.md")


def test_malformed_host_entry_is_no_state(env):
    env.state.mkdir(parents=True)
    (env.state / "repos-state.json").write_text(json.dumps(
        {"hosts": {"tig-adi": {"date": "2026-11-20", "checkouts": [{"path": "/x"}]}}}))
    env.fail_ssh("tig-adi")
    assert env.run_repos() == 0, env.err()
    assert "tig-adi paths unknown (unreachable today)" in env.page("repos.md")


@pytest.mark.parametrize("breakage", ["pull_fails", "github_down", "github_empty", "no_wiki", "secret", "private_leak"])
def test_aborts_write_nothing(env, breakage):
    env.break_(breakage)
    assert env.run_repos() == 1 and env.puts == [] and not (env.state / "repos-state.json").exists()
    assert env.err().strip()


@pytest.mark.parametrize("breakage", ["pull_fails", "github_down", "github_empty", "no_wiki", "secret", "private_leak"])
def test_aborts_also_in_dry_run(env, breakage):
    env.break_(breakage)
    assert env.run_repos(dry_run=True) == 1 and env.out() == ""


def test_pull_skipped_when_config_says_no(env):
    env.cfg = replace(env.cfg, pull=False)
    env.fake.pull_code = 1
    assert env.run_repos() == 0
    assert not any(c[:2] == ["git", "-C"] for c in env.fake.calls)


def test_private_leak_message_names_name_and_row(env):
    env.break_("private_leak")
    env.run_repos()
    assert "valuebook" in env.err() and "FibonAdithya/chain" in env.err()


def test_bare_private_override_moves_local_checkout_to_private_page(env):
    # `private = ["Ledger"]` lacks the `local:` prefix; a bare name still moves the row.
    env.overrides.write_text('private = ["Ledger"]\n')
    env.fake.laptop.append(("/home/u/code/Ledger", "", "4", "2026-11-12"))
    assert env.run_repos() == 0
    assert "ledger" not in env.page("repos.md").lower() and "Ledger (local only)" in env.page("repos-private.md")


def test_missing_overrides_file_exits_2_and_writes_nothing(env):
    # the file names private repos with no wiki page; without it one could land on the public page
    env.overrides.unlink()
    env.fake.laptop.append(("/home/u/code/MoneyNotes", "", "4", "2026-11-12"))
    assert env.run_repos() == 2 and env.puts == [] and "overrides file missing" in env.err()
    assert not (env.state / "repos-state.json").exists()


def test_local_private_override_moves_local_repo(env):
    env.overrides.write_text('private = ["local:MoneyNotes"]\n')
    env.fake.laptop.append(("/home/u/code/MoneyNotes", "", "4", "2026-11-12"))
    assert env.run_repos() == 0
    assert "moneynotes" not in env.page("repos.md").lower()
    assert "MoneyNotes (local only)" in env.page("repos-private.md")


def test_moneynotes_with_github_origin_private_and_exit_0(env):
    # once MoneyNotes is pushed, the override `local:MoneyNotes` still hides it, and the
    # final check (which scrubs by bare name) agrees with the row split, so the run succeeds
    env.overrides.write_text('private = ["local:MoneyNotes"]\n')
    env.fake.owned["FibonAdithya/MoneyNotes"] = node("FibonAdithya/MoneyNotes", "Stocks.", 4, "2026-11-12")
    env.fake.laptop.append(("/home/u/code/MoneyNotes", "git@github.com:FibonAdithya/MoneyNotes.git", "", ""))
    assert env.run_repos() == 0, env.err()
    assert "moneynotes" not in env.page("repos.md").lower()
    assert "FibonAdithya/MoneyNotes" in env.page("repos-private.md")


def test_secret_message_names_page_not_secret(env):
    env.break_("secret")
    env.run_repos()
    assert "repos.md" in env.err() and "abcd1234" not in env.err()


def test_bad_overrides_exit_2(env):
    env.overrides.write_text("private = 3\n")
    assert env.run_repos() == 2 and env.puts == []


def test_pull_runs_even_with_a_future_stamp(env):
    env.state.mkdir(parents=True)
    stamp = env.state / "last-pull"
    stamp.write_text("")
    future = env.now.timestamp() + 3600
    os.utime(stamp, (future, future))
    assert env.run_repos() == 0, env.err()
    assert ["git", "-C", str(env.root), "pull", "--ff-only", "-q"] in env.fake.calls


def test_pull_timeout_exits_1(env):
    env.fake.pull_code = "timeout"
    assert env.run_repos() == 1 and env.puts == [] and "pull" in env.err()


def test_unreadable_overrides_exit_2(env):
    env.overrides.unlink()
    env.overrides.mkdir()
    assert env.run_repos() == 2 and env.puts == [] and "overrides" in env.err()


def test_empty_owners_exit_2(env):
    env.overrides.write_text("owners = []\n")
    assert env.run_repos() == 2 and env.puts == []
    assert "owners must not be empty" in env.err()
    assert not any("--paginate" in c for c in env.fake.calls)


def test_state_save_failure_exits_1_after_pages_written(env):
    env.state.parent.mkdir(parents=True, exist_ok=True)
    env.state.write_text("not a directory")
    assert env.run_repos() == 1
    assert env.puts == ["repos.md", "repos-private.md"]
    assert "pages written; state not saved:" in env.err() and "Traceback" not in env.err()


def test_help_says_dry_run_still_pulls(capsys):
    with pytest.raises(SystemExit):
        cli.main(["repos", "--help"])
    assert "--dry-run still pulls the wiki clone; it writes no page and no state." in \
        " ".join(capsys.readouterr().out.split())


def test_dry_run_prints_and_writes_nothing(env):
    assert env.run_repos(dry_run=True) == 0
    assert "| repo | purpose |" in env.out() and env.puts == []
    assert "FibonAdithya/valuebook" in env.out()  # both pages are printed
    assert not (env.state / "repos-state.json").exists()


def test_one_lookup_failure_marks_row_only(env):
    env.fail_lookup("tig-foundation/tig-knowledge-base")
    assert env.run_repos() == 0
    assert "lookup failed" in env.page("repos.md") and "FibonAdithya/chain" in env.page("repos.md")


def test_lookups_only_unowned_slugs_once_each(env):
    env.overrides.write_text('include = ["local:toy", "Tig-Foundation/Talos", "someone/else", '
                             '"fibonadithya/chain"]\n')
    assert env.run_repos() == 0, env.err()
    looked = sorted(next(a for a in c if a.startswith("owner=")) + "/" +
                    next(a for a in c if a.startswith("name=")) for c in env.fake.lookups())
    assert [s.lower() for s in looked] == ["owner=someone/name=else", "owner=tig-foundation/name=talos",
                                           "owner=tig-foundation/name=tig-knowledge-base"]


def test_store_failure_exits_1_without_state_and_next_run_retries(env):
    env.store.fail_on = {"repos-private.md"}
    assert env.run_repos() == 1
    assert env.puts == ["repos.md"] and "HTTP 500" in env.err()
    assert not (env.state / "repos-state.json").exists()
    env.store.fail_on = set()
    assert env.run_repos() == 0
    assert env.puts == ["repos.md", "repos-private.md"]


def test_cli_registers_repos(monkeypatch, tmp_path):
    with pytest.raises(SystemExit) as e:
        cli.main(["repos", "--help"])
    assert e.value.code == 0


def test_cli_dry_run_flag_reaches_run_repos(env):
    args = cli.build_parser().parse_args(["repos", "--dry-run", "--overrides", str(env.overrides)])
    out, err = io.StringIO(), io.StringIO()
    code = cli._cmd_repos(args, env.cfg, run=env.fake, store=env.store, now=env.now, out=out, err=err)
    assert code == 0, err.getvalue()
    assert "| repo | purpose |" in out.getvalue() and env.puts == []


def test_cli_without_dry_run_writes(env):
    args = cli.build_parser().parse_args(["repos", "--overrides", str(env.overrides)])
    code = cli._cmd_repos(args, env.cfg, run=env.fake, store=env.store, now=env.now,
                          out=io.StringIO(), err=io.StringIO())
    assert code == 0 and env.puts == ["repos.md", "repos-private.md"]
