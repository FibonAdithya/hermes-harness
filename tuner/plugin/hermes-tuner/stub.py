"""Runs one composed-tool program inside the sandbox container.

Wire protocol, one JSON object per line:
  host -> stub: {"program": str, "args": dict}, then {"result": str} per call
  stub -> host: {"call": name, "args": dict}, {"done": value} or {"error": str}

The program's own stdout is redirected to stderr so a print() cannot corrupt
the protocol. This is not a security boundary: the host checks every call.
Standard library only; the relay passes this file's text to `python3 -c`.
"""
import json
import os
import sys


def _run():
    proto_in = sys.stdin
    proto_out = sys.stdout
    sys.stdout = sys.stderr
    sys.stdin = open(os.devnull)

    def send(obj):
        proto_out.write(json.dumps(obj) + "\n")
        proto_out.flush()

    def call(name, args=None):
        send({"call": name, "args": {} if args is None else args})
        line = proto_in.readline()
        if not line:
            raise RuntimeError("host closed the channel")
        result = json.loads(line)["result"]
        try:
            return json.loads(result)
        except (TypeError, ValueError):
            return result

    try:
        request = json.loads(proto_in.readline())
        namespace = {"__name__": "__tuned__"}
        exec(compile(request["program"], "<program>", "exec"), namespace)
        value = namespace["main"](request.get("args") or {}, call)
        try:
            line = json.dumps({"done": value})
        except (TypeError, ValueError) as exc:
            send({"error": f"result is not JSON-serialisable: {exc}"})
            return
        proto_out.write(line + "\n")
        proto_out.flush()
    except BaseException as exc:  # noqa: BLE001 - every failure goes back to the host
        try:
            send({"error": f"{type(exc).__name__}: {exc}"})
        except BaseException:  # noqa: BLE001
            pass


if __name__ == "__main__":
    _run()
