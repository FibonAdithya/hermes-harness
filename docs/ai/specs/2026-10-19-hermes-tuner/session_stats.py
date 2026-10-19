"""Read-only aggregate stats over Hermes state.db. Prints counts and sums only, never message text."""
import collections
import json
import sqlite3
import time

db = sqlite3.connect("file:/home/hermes/.hermes/state.db?mode=ro", uri=True)
q = lambda s, *a: db.execute(s, a).fetchall()
month = lambda t: time.strftime("%Y-%m", time.gmtime(t))

print("== range")
lo, hi = q("select min(started_at), max(started_at) from sessions")[0]
print(time.strftime("%Y-%m-%d", time.gmtime(lo)), "->", time.strftime("%Y-%m-%d", time.gmtime(hi)))

print("\n== sessions by source and month: n, api-side tokens (M), est $, actual $")
rows = q("select source, started_at, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,"
         " coalesce(estimated_cost_usd,0), coalesce(actual_cost_usd,0) from sessions")
agg = collections.defaultdict(lambda: [0, 0, 0, 0, 0, 0.0, 0.0])
for src, t, i, o, cr, cw, e, a in rows:
    k = (month(t), src)
    v = agg[k]
    v[0] += 1; v[1] += i or 0; v[2] += o or 0; v[3] += cr or 0; v[4] += cw or 0; v[5] += e; v[6] += a
for k in sorted(agg):
    n, i, o, cr, cw, e, a = agg[k]
    print(f"{k[0]} {k[1]:10} n={n:4} in={i/1e6:7.2f}M out={o/1e6:6.2f}M cache_read={cr/1e6:7.2f}M"
          f" cache_write={cw/1e6:6.2f}M est=${e:7.2f} actual=${a:7.2f}")

print("\n== cost_status values")
for r in q("select cost_status, count(*) from sessions group by 1"):
    print(r)

print("\n== model usage by model and task (all time)")
for r in q("select model, task, count(*), sum(api_call_count), sum(input_tokens), sum(output_tokens),"
           " sum(cache_read_tokens), round(sum(estimated_cost_usd),2), round(sum(actual_cost_usd),2)"
           " from session_model_usage group by 1,2 order by 5 desc"):
    print(r)

print("\n== system prompt size (chars) by source, last 30 days")
cut = hi - 30 * 86400
for r in q("select source, count(*), avg(length(system_prompt)), max(length(system_prompt))"
           " from sessions where started_at > ? and system_prompt is not null group by 1", cut):
    print(r)

print("\n== tool calls by name (all time; result chars = context they added)")
tool = collections.Counter(); tool_chars = collections.Counter()
for name, n, chars in q("select tool_name, count(*), sum(length(content)) from messages"
                        " where role='tool' group by 1"):
    tool[name] = n; tool_chars[name] = chars or 0
for name, n in tool.most_common(40):
    print(f"{str(name):34} calls={n:6} result_chars={tool_chars[name]:>11,} avg={tool_chars[name]//max(n,1):>7,}")

print("\n== skill_view loads by skill, and the size of what came back")
loads = collections.Counter(); load_chars = collections.Counter()
# map tool_call_id -> skill name from assistant tool_calls
id2skill = {}
for (tc,) in q("select tool_calls from messages where role='assistant' and tool_calls like '%skill_view%'"):
    try:
        for c in json.loads(tc):
            fn = c.get("function") or {}
            if fn.get("name") == "skill_view":
                args = fn.get("arguments")
                args = json.loads(args) if isinstance(args, str) else (args or {})
                id2skill[c.get("id")] = args.get("name", "?") + ("/" + args["file_path"] if args.get("file_path") else "")
    except (ValueError, TypeError, AttributeError):
        pass
for cid, chars in q("select tool_call_id, length(content) from messages where role='tool' and tool_name='skill_view'"):
    s = id2skill.get(cid, "?")
    loads[s] += 1; load_chars[s] += chars or 0
for s, n in loads.most_common(30):
    print(f"{s:50} loads={n:4} total_chars={load_chars[s]:>9,} per_load={load_chars[s]//n:>7,}")

print("\n== repeated tool-call sequences (names only), per session, n=3..5")
seqs = collections.defaultdict(list)
for sid, name in q("select session_id, tool_name from messages where role='tool' order by session_id, id"):
    seqs[sid].append(name)
for n in (3, 4, 5):
    grams = collections.Counter(); sess_with = collections.defaultdict(set)
    for sid, s in seqs.items():
        for i in range(len(s) - n + 1):
            g = tuple(s[i:i + n])
            if len(set(g)) == 1 and g[0] in ("terminal",):
                pass  # keep, but they are common; still counted
            grams[g] += 1; sess_with[g].add(sid)
    print(f"-- n={n}")
    for g, c in grams.most_common(12):
        print(f"   x{c:5} in {len(sess_with[g]):4} sessions: {' > '.join(map(str, g))}")
