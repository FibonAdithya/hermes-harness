---
name: delegate-with-context
description: Before handing work to the coding box (run_task) or to a subagent (delegate_task), find the wiki pages that help and pass them along.
---

# Delegate with context

Use this every time you call `run_task` or `delegate_task`. The wiki at `~/hermes-wiki` holds what the owner and their agents already know: how machines are set up, past decisions, gotchas in each repo. A worker that gets the right pages up front repeats less work and makes fewer wrong guesses.

## Find the pages

1. Pick two or three key words from the task: the repo name, the service, the problem.
2. Search the wiki, for example `rg -il 'talos' ~/hermes-wiki --glob '!inbox/**'`. To narrow by tag, `rg -l '^tags:.*\bfleet\b' ~/hermes-wiki --glob '!inbox/**'`.
3. Open the most likely hits and keep only pages that would change what the worker does. Fewer, relevant pages beat many loose ones.

A page name is its path inside the wiki without `.md`, for example `hermes-harness` or `nights/2026-10-25`.

## Before `run_task`

Pass up to 10 page names as `context_pages`. The box attaches their text to the brief as reference data, up to 6,000 words in total. Do not paste page text into `prompt` yourself.

The box only has the public copy of the wiki. Private pages (`type: person`, or tagged `finance`, `investing`, `health` or `people`, or without frontmatter) never reach it, and the reply lists any page it did not attach. If the task really needs something from a private page, summarise only that fact in your own words in `prompt`, and only if it is safe to appear in a pull request, which other people may read. Otherwise leave it out.

## Before `delegate_task`

Subagents share your sandbox and can read the wiki, but they do not know which pages matter. `delegate_task` takes a `goal` and a `context`: put the relevant page names and the key facts in `context`, so the subagent starts from what is already known.
