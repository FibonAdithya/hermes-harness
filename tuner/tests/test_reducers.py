import json

from hermes_tuner.reducers import OPS, apply_reducer, text_view, validate_spec

T = "mcp__google__get_gmail_message_content"
J = "cc68e24f21cd"


def spec(*steps):
    return {"tool": T, "job": J, "steps": list(steps)}


def test_shorten_urls_keeps_meeting_hosts():
    t = "join https://us02web.zoom.us/j/123?pwd=x or see https://tracker.example.com/a/b?c=d"
    out = OPS["shorten_urls"](t, {"keep_hosts": []})
    assert "https://us02web.zoom.us/j/123?pwd=x" in out
    assert "tracker.example.com" in out and "/a/b?c=d" not in out


def test_shorten_urls_exact_and_keep_hosts_dot_boundary():
    t = "a https://x.example.com/p?q=1 b https://keep.org/z c https://evilzoom.us/j"
    out = OPS["shorten_urls"](t, {"keep_hosts": ["keep.org"]})
    assert out == "a x.example.com b https://keep.org/z c evilzoom.us"


def test_drop_footer_only_in_tail():
    body = "Unsubscribe from nothing, this is the first line.\n" + "body\n" * 50 + "To unsubscribe click here\nlegal"
    out = OPS["drop_footer"](body, {})
    assert out.startswith("Unsubscribe from nothing") and "legal" not in out
    assert out.endswith("body\n")


def test_html_to_text():
    h = "<html><style>p{color:red}</style><script>var x=1;</script><p>Hi <b>there</b></p><div>Next &amp; last</div></html>"
    assert OPS["html_to_text"](h, {}) == "Hi there\nNext & last"


def test_drop_quoted():
    t = "keep\n> quoted\nalso keep\nOn Mon, 5 Oct 2026, Bob <b@x.com> wrote:\n> old\nmore old"
    assert OPS["drop_quoted"](t, {}) == "keep\nalso keep\n"


def test_collapse_ws():
    assert OPS["collapse_ws"]("a   b \t c  \n\n\n\nd", {}) == "a b c\n\nd"


def test_cap():
    assert OPS["cap"]("abcdef", {"chars": 4}) == "abcd [cut]"
    assert OPS["cap"]("abcd", {"chars": 4}) == "abcd"
    assert OPS["cap"]("abc", {"chars": 4}) == "abc"


def test_apply_reducer_json_leaves():
    r = json.dumps({"content": "<p>Hi <b>there</b></p>", "id": "m1", "n": 3, "headers": {"x": "y"}})
    s = spec({"op": "html_to_text"}, {"op": "drop_fields", "fields": ["headers"]})
    out = json.loads(apply_reducer(s, r))
    assert out == {"content": "Hi there", "id": "m1", "n": 3}


def test_apply_reducer_keeps_non_ascii_unescaped():
    r = json.dumps({"a": "café   ok"}, ensure_ascii=False)
    out = apply_reducer(spec({"op": "collapse_ws"}), r)
    assert out == '{"a": "café ok"}'


def test_apply_reducer_odd_inputs():
    s = spec({"op": "collapse_ws"})
    assert apply_reducer(s, '{"a": "x   y"') == '{"a": "x y"'
    assert apply_reducer(s, "null") == "null"
    assert apply_reducer(s, "plain   text") == "plain text"
    assert apply_reducer(s, '{"a": 1, "b": null, "c": [2, "p   q"]}') == '{"a": 1, "b": null, "c": [2, "p q"]}'
    html = spec({"op": "html_to_text"})
    assert apply_reducer(html, "<br>") == "<br>"
    drop = spec({"op": "drop_fields", "fields": ["a"]})
    assert apply_reducer(drop, '{"a": "x"}') == '{"a": "x"}'


def test_apply_reducer_never_raises():
    assert apply_reducer({"steps": "nope"}, "abc") == "abc"
    assert apply_reducer({"steps": [{"op": "cap", "chars": "x"}]}, "abc") == "abc"
    assert apply_reducer(None, "abc") == "abc"
    assert apply_reducer(spec({"op": "bogus"}), "abc") == "abc"


def test_drop_fields_top_level_only():
    r = json.dumps({"result": "body", "structuredContent": {"result": "body"}})
    s = spec({"op": "drop_fields", "fields": ["structuredContent"]})
    assert json.loads(apply_reducer(s, r)) == {"result": "body"}
    s2 = spec({"op": "drop_fields", "fields": ["result"]})
    assert json.loads(apply_reducer(s2, r)) == {"structuredContent": {"result": "body"}}


def test_text_view():
    assert text_view(json.dumps({"b": "two\nlines", "a": "one", "n": 3})) == "one\ntwo\nlines"
    assert text_view("not json") == "not json"
    assert text_view(json.dumps({"z": {"y": "2", "x": "1"}, "a": ["p", 5, "q"]})) == "p\nq\n1\n2"


def test_validate_spec():
    good = spec({"op": "html_to_text"}, {"op": "cap", "chars": 8000},
                {"op": "drop_fields", "fields": ["structuredContent"]})
    assert validate_spec(good) == []
    assert validate_spec(spec({"op": "shorten_urls", "keep_hosts": ["a.com"]})) == []
    assert validate_spec(spec({"op": "eval"}))
    assert validate_spec(spec({"op": "cap", "chars": 123}))
    assert validate_spec({"tool": "web_extract", "job": J, "steps": []})
    assert validate_spec({"tool": T, "job": "3bc4e11d29b1", "steps": []})
    assert validate_spec(spec({"op": "collapse_ws", "x": 1}))
    assert validate_spec(spec({"op": "cap", "chars": 8000}, {"op": "cap", "chars": 4000}))
    assert validate_spec(spec({"op": "collapse_ws"}, {"op": "collapse_ws"}))
    assert validate_spec(spec("collapse_ws"))
    assert validate_spec(spec({"op": "cap", "chars": True}))
    assert validate_spec(spec({"op": "drop_fields", "fields": "a"}))
    assert validate_spec("nope") and validate_spec(None)
    assert validate_spec({"tool": T, "job": J})


def test_html_to_text_keeps_angle_addresses_and_urls():
    f = OPS["html_to_text"]
    assert "<bob@example.com>" in f("<p>From: Bob <bob@example.com></p>", {})
    assert "<https://meet.google.com/abc-def>" in f("<div>Meet: <https://meet.google.com/abc-def></div>", {})
    assert f("From: Bob <bob@example.com>", {}) == "From: Bob <bob@example.com>"
    assert f("<b@x.com>", {}) == "<b@x.com>"


def test_html_to_text_plain_text_unchanged():
    t = "  line one\n\n\n    indented <a@b.co>\n\nend  "
    assert OPS["html_to_text"](t, {}) == t


def test_validate_spec_ignores_top_level_base():
    """Fix payloads carry `_base` (the job as drafted against) next to the spec."""
    spec = {"tool": "mcp__google__get_events", "job": "cc68e24f21cd",
            "steps": [{"op": "collapse_ws"}],
            "_base": {"prompt": "p", "enabled_toolsets": None}}
    assert validate_spec(spec) == []
