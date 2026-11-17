"""A stand-in for the gh CLI, shared by the tasklib, verb and doctor tests."""
import json

SLUG = "FibonAdithya/fleet-fixture"


class FakeGh:
    """Stands in for the gh CLI. Labels must exist before they are applied, as with real gh.

    Comments and assignees have gh's --json shape: [{"author": {"login": ...}, "body": ...}]
    and [{"login": ...}]; a comment posted through the fake is authored by the box login.
    `issue list` and `issue view` return only the fields named in --json, as gh does."""

    def __init__(self):
        self.login = "fibon-box"
        self.labels = {"fleet:triage", "fleet:ready", "fleet:auto-ok", "fleet:human", "area:docs", "class:patch"}
        self.issues = {}
        self.calls = []

    def __call__(self, args, stdin=None):
        self.calls.append((tuple(args), stdin))
        cmd = args[:2]
        if cmd == ["label", "create"]:
            if args[2] in self.labels:
                return 1, "", f"label with name \"{args[2]}\" already exists; use `--force` to update its color and description"
            self.labels.add(args[2])
            return 0, "", ""
        if cmd == ["issue", "create"]:
            names = [args[i + 1] for i, a in enumerate(args) if a == "--label"]
            missing = [n for n in names if n not in self.labels]
            if missing:
                return 1, "", f"could not add label: '{missing[0]}' not found"
            n = len(self.issues) + 1
            title = args[args.index("--title") + 1]
            self.issues[n] = {"number": n, "title": title, "body": stdin, "state": "OPEN",
                              "labels": [{"name": x} for x in names], "comments": [], "assignees": []}
            return 0, f"https://github.com/{SLUG}/issues/{n}\n", ""
        if cmd == ["issue", "view"]:
            i = self.issues.get(int(args[2]))
            return (0, json.dumps(_fields(i, args)), "") if i else (1, "", "not found")
        if cmd == ["issue", "edit"]:
            i = self.issues[int(args[2])]
            if "--add-label" in args:
                for n in args[args.index("--add-label") + 1].split(","):
                    if n not in self.labels:
                        return 1, "", f"'{n}' not found"
                    if n not in [x["name"] for x in i["labels"]]:
                        i["labels"].append({"name": n})
            if "--remove-label" in args:
                gone = set(args[args.index("--remove-label") + 1].split(","))
                i["labels"] = [x for x in i["labels"] if x["name"] not in gone]
            if "--add-assignee" in args:
                for n in args[args.index("--add-assignee") + 1].split(","):
                    if n not in [x["login"] for x in i["assignees"]]:
                        i["assignees"].append({"login": n})
            if "--remove-assignee" in args:
                gone = set(args[args.index("--remove-assignee") + 1].split(","))
                i["assignees"] = [x for x in i["assignees"] if x["login"] not in gone]
            return 0, "", ""
        if cmd == ["issue", "close"]:
            self.issues[int(args[2])]["state"] = "CLOSED"
            return 0, "", ""
        if cmd == ["issue", "list"]:
            label = args[args.index("--label") + 1]
            rows = [i for i in self.issues.values() if i["state"] == "OPEN" and label in [x["name"] for x in i["labels"]]]
            return 0, json.dumps([_fields(i, args) for i in rows]), ""
        if cmd == ["issue", "comment"]:
            self.issues[int(args[2])]["comments"].append({"author": {"login": self.login},
                                                          "body": args[args.index("--body") + 1]})
            return 0, "", ""
        if args == ["api", "user", "--jq", ".login"]:
            return 0, self.login + "\n", ""
        raise AssertionError(f"unexpected gh call {args}")


def _fields(issue, args):
    want = args[args.index("--json") + 1].split(",")
    return {k: issue[k] for k in want if k in issue}
