"""Scan checkouts of the owner's repos on each host, locally or over ssh.

Every shell-out goes through an injected `run` with subprocess.run's signature.
Origin URLs are parsed to an owner/name slug here and never leave this module.
"""
import json
import re
import shlex
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .pages import iter_pages
from .schema import is_private, load_tags


@dataclass(frozen=True)
class Checkout:
    host: str
    path: str
    slug: str | None
    local_name: str
    commits_30d: int | None
    last_commit: str | None


@dataclass(frozen=True)
class Host:
    name: str
    ssh: str | None
    root: str  # relative to the host's $HOME
    depth: int
    mode: str  # "git" or "names"
    display_root: str | None  # replaces the scanned root in rendered paths


HOSTS: tuple[Host, ...] = (
    Host("laptop", None, "", 3, "git", None),
    Host("tig-adi", "tig-adi", "", 3, "git", None),
    Host("hermes-vm", "hermes-vm", ".hermes/sandboxes/docker/default/home/repos", 1,
         "names", "/root/repos"),
)

SSH_TIMEOUT = 20

# args: <root-rel-to-HOME> <depth> <mode> <since-YYYY-MM-DD>
SCAN_SH = r"""
root="$HOME"
if [ -n "$1" ]; then root="$HOME/$1"; fi
depth="$2"; mode="$3"; since="$4"
[ -d "$root" ] || exit 3
printf 'root\t%s\n' "$root"
if [ "$mode" = names ]; then
  n=0
  for e in "$root"/*; do
    [ -e "$e" ] || continue
    printf 'name\t%s\n' "${e##*/}"
    n=$((n + 1))
  done
  printf 'end\t%s\n' "$n"
  exit 0
fi
find "$root" -maxdepth "$depth" \
  \( -name .cache -o -name node_modules -o -name Trash -o -name .worktrees \) -prune -o \
  -name .git -type d -prune -print 2>/dev/null | {
  n=0
  while IFS= read -r g; do
    d="${g%/.git}"
    origin=$(git -C "$d" config --get remote.origin.url 2>/dev/null)
    count=""; last=""
    if git -C "$d" rev-parse --git-dir >/dev/null 2>&1 &&
       ! git -C "$d" rev-parse --verify -q HEAD >/dev/null 2>&1; then
      count=0  # unborn: a repo with no commits is dormant, not a failed lookup
    else
      ref=HEAD
      for b in main master; do
        if git -C "$d" rev-parse --verify -q "refs/heads/$b" >/dev/null 2>&1; then ref="$b"; break; fi
      done
      count=$(git -C "$d" rev-list --count --since="$since" "$ref" 2>/dev/null)
      last=$(git -C "$d" log -1 --format=%cs "$ref" 2>/dev/null)
    fi
    printf 'repo\t%s\t%s\t%s\t%s\n' "$d" "$origin" "$count" "$last"
    n=$((n + 1))
  done
  printf 'end\t%s\n' "$n"
}
"""


class Unreachable(Exception):
    pass


_GITHUB = re.compile(r"^(?:https?://(?:[^/@]+@)?github\.com/|(?:ssh://)?git@github\.com[:/])"
                     r"([^/\s]+)/([^/\s]+?)(?:\.git)?/?$")


def parse_slug(url: str) -> str | None:
    m = _GITHUB.match(url.strip())
    return f"{m.group(1)}/{m.group(2)}" if m else None


def _argv(host: Host, since: str) -> list[str]:
    args = ["bash", "-s", "--", host.root, str(host.depth), host.mode, since]
    if host.ssh is None:
        return args
    # ssh joins the remaining argv into one remote command string, so an empty or
    # spaced argument must be quoted or every later argument shifts.
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host.ssh,
            *[shlex.quote(a) for a in args]]


def _int_or_none(s: str) -> int | None:
    return int(s) if s.isdigit() else None


def scan_host(host: Host, run, since: str) -> list[Checkout]:
    try:
        r = run(_argv(host, since), input=SCAN_SH, capture_output=True, text=True,
                timeout=SSH_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError) as e:
        raise Unreachable(f"{host.name}: {e}") from e
    if r.returncode != 0:
        raise Unreachable(f"{host.name}: exit {r.returncode}")
    lines = (r.stdout or "").splitlines()
    if not any(line.startswith("root\t") for line in lines):
        raise Unreachable(f"{host.name}: no root header in output")
    ends = [line.split("\t") for line in lines if line.startswith("end\t")]
    seen = sum(1 for line in lines if line.startswith(("repo\t", "name\t")))
    if len(ends) != 1 or len(ends[0]) != 2 or ends[0][1] != str(seen):
        raise Unreachable(f"{host.name}: truncated output (missing or wrong end line)")
    out: list[Checkout] = []
    for line in lines:
        f = line.split("\t")
        if f[0] == "name" and len(f) == 2 and host.mode == "names" and f[1]:
            base = host.display_root if host.display_root is not None else ""
            out.append(Checkout(host.name, f"{base}/{f[1]}", None, f[1], None, None))
        elif f[0] == "repo" and len(f) == 5 and host.mode == "git" and f[1]:
            path = f[1]
            slug = parse_slug(f[2])
            count = last = None
            if slug is None:
                count = _int_or_none(f[3])
                last = f[4] or None
            out.append(Checkout(host.name, path, slug, path.rstrip("/").rsplit("/", 1)[-1],
                                count, last))
    return out


@dataclass(frozen=True)
class GhRepo:
    slug: str
    description: str
    fork: bool
    archived: bool
    commits_30d: int | None
    last_commit: str | None


class GitHubDown(Exception):
    pass


_FIELDS = ("nameWithOwner description isFork isArchived defaultBranchRef{target{... on Commit"
           "{committedDate history(since:$since){totalCount}}}}")
OWNER_QUERY = ("query($owner:String!,$since:GitTimestamp!,$endCursor:String){"
               "repositoryOwner(login:$owner){repositories(first:100,after:$endCursor,ownerAffiliations:[OWNER]){"
               "nodes{" + _FIELDS + "} pageInfo{hasNextPage endCursor}}}}")
OWNER_JQ = ".data.repositoryOwner.repositories.nodes[]"
REPO_QUERY = ("query($owner:String!,$name:String!,$since:GitTimestamp!){"
              "repository(owner:$owner,name:$name){" + _FIELDS + "}}")
REPO_JQ = ".data.repository"

OWNER_TIMEOUT = 60
REPO_TIMEOUT = 30


def _node_to_repo(node: dict) -> GhRepo:
    ref = node.get("defaultBranchRef")
    if not ref:
        count, last = 0, None  # empty repo: no default branch
    else:
        target = ref["target"]
        count = target["history"]["totalCount"]
        last = target["committedDate"][:10]
    return GhRepo(node["nameWithOwner"], node.get("description") or "",
                  bool(node["isFork"]), bool(node["isArchived"]), count, last)


def owner_repos(owner: str, run, since_iso: str, gh: str = "gh") -> list[GhRepo]:
    argv = [gh, "api", "graphql", "--paginate", "-f", f"owner={owner}", "-f", f"since={since_iso}",
            "-f", f"query={OWNER_QUERY}", "--jq", OWNER_JQ]
    try:
        r = run(argv, capture_output=True, text=True, timeout=OWNER_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError) as e:
        raise GitHubDown(f"gh: {e}") from e
    if r.returncode != 0:
        raise GitHubDown(f"gh: exit {r.returncode}")
    repos = []
    for line in (r.stdout or "").splitlines():
        if not line.strip():
            continue
        try:
            repo = _node_to_repo(json.loads(line))
        except (ValueError, KeyError, TypeError, AttributeError) as e:
            raise GitHubDown(f"gh: unparseable repo line ({type(e).__name__})") from e
        # defence in depth: never list a repo another owner holds
        if repo.slug.partition("/")[0].lower() == owner.lower():
            repos.append(repo)
    if not repos:
        raise GitHubDown("gh: owner listing returned zero repos")
    return repos


def one_repo(slug: str, run, since_iso: str, gh: str = "gh") -> GhRepo | None:
    owner, sep, name = slug.partition("/")
    if not sep or not owner or not name:
        return None
    argv = [gh, "api", "graphql", "-f", f"owner={owner}", "-f", f"name={name}",
            "-f", f"since={since_iso}", "-f", f"query={REPO_QUERY}", "--jq", REPO_JQ]
    try:
        r = run(argv, capture_output=True, text=True, timeout=REPO_TIMEOUT)
        if r.returncode != 0:
            return None
        return _node_to_repo(json.loads(r.stdout))
    except (subprocess.TimeoutExpired, OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


# --- wiki page index, README lines, overrides ---

PURPOSE_MAX = 160
_SENTENCE_END = re.compile(r"[.!?](?:\s|$)")


def _cap(text: str) -> str:
    return text if len(text) <= PURPOSE_MAX else text[: PURPOSE_MAX - 1] + "…"


@dataclass(frozen=True)
class PageRef:
    name: str
    private: bool
    purpose: str


def _first_sentence(body: str) -> str:
    """First sentence of the first paragraph that is not a heading or an HTML comment."""
    para: list[str] = []
    in_comment = False
    for raw in body.splitlines():
        line = raw.strip()
        if in_comment:
            if "-->" in line:
                in_comment = False
                line = line.split("-->", 1)[1].strip()
            else:
                continue
        while line.startswith("<!--"):
            if "-->" in line:
                line = line.split("-->", 1)[1].strip()
            else:
                in_comment = True
                line = ""
        if in_comment:
            continue
        if not line:
            if para:
                break
            continue
        if line.startswith("#"):
            if para:
                break
            continue
        para.append(line)
    text = " ".join(" ".join(para).split())
    m = _SENTENCE_END.search(text)
    if m:
        text = text[: m.start() + 1]
    return _cap(text)


# a `repos:` entry is a GitHub name or owner/name; anything else is a malformed list (a
# missing `]` or comma) whose key would miss the real repo name and leak it
_REPO_KEY = re.compile(r"[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)?")


def page_index(root: Path) -> dict[str, list[PageRef]]:
    """Map lower-cased repo name to the pages naming it. Raises OSError on an unusable wiki."""
    root = Path(root)
    if not root.is_dir():
        raise OSError(f"wiki root is not a directory: {root}")
    pages = iter_pages(root)
    if not pages:
        raise OSError(f"no pages under {root}")
    unparsed = [p.name for p in pages if not p.infra and p.meta is None]
    if unparsed:
        raise OSError(f"unparsed frontmatter, cannot classify: {', '.join(unparsed)}")
    allowed = load_tags(root)
    index: dict[str, list[PageRef]] = {}
    for page in pages:  # iter_pages sorts by name
        repos = page.meta.get("repos") if page.meta else None
        if isinstance(repos, str):
            repos = repos.split(",")
        if not isinstance(repos, list):
            continue
        ref = PageRef(page.name, is_private(page.meta, allowed_tags=allowed), _first_sentence(page.body))
        for repo in repos:
            repo = str(repo).strip()
            if repo and not _REPO_KEY.fullmatch(repo):
                raise OSError(f"{page.name}: unparseable repos entry {repo!r}")
            if repo:
                refs = index.setdefault(repo.lower(), [])
                if all(r.name != ref.name for r in refs):
                    refs.append(ref)
    return index


def readme_line(path: str) -> str | None:
    for fname in ("README.md", "README.rst", "README"):
        try:
            text = (Path(path) / fname).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines = [raw.strip() for raw in text.splitlines()]
        in_comment = False
        for i, line in enumerate(lines):
            if in_comment:
                in_comment = "-->" not in line
                continue
            if line.startswith("<!--"):
                in_comment = "-->" not in line
                continue
            if not line or line.startswith(("#", "[![")):
                continue
            if set(line) <= {"=", "-"}:  # RST underline
                continue
            if i + 1 < len(lines) and len(lines[i + 1]) >= 3 and set(lines[i + 1]) <= {"=", "-"}:
                continue  # RST title text
            return _cap(line)
    return None


@dataclass(frozen=True)
class Overrides:
    owners: tuple[str, ...] = ("FibonAdithya",)
    clone_owners: tuple[str, ...] = ("FibonAdithya", "tig-foundation", "AIDDA-Institute")
    include: frozenset[str] = frozenset()
    exclude: frozenset[str] = frozenset()
    private: frozenset[str] = frozenset()
    purpose: dict[str, str] = field(default_factory=dict)


def _strs(key: str, value) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"{key}: must be a list of strings")
    return value


def load_overrides(path: Path) -> Overrides:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return Overrides()
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"{path}: {e}") from e
    known = {"owners", "clone_owners", "include", "exclude", "private", "purpose"}
    unknown = sorted(set(data) - known)
    if unknown:
        raise ValueError(f"{path}: unknown keys {unknown}")
    kwargs: dict = {}
    for key in ("owners", "clone_owners"):
        if key in data:
            kwargs[key] = tuple(_strs(key, data[key]))
    for key in ("include", "exclude", "private"):
        if key in data:
            kwargs[key] = frozenset(_strs(key, data[key]))
    if "purpose" in data:
        purpose = data["purpose"]
        if not isinstance(purpose, dict) or not all(isinstance(v, str) for v in purpose.values()):
            raise ValueError("purpose: must be a table of strings")
        kwargs["purpose"] = dict(purpose)
    return Overrides(**kwargs)
