# Content jobs

Hermes starts content jobs on the box with `run_task`, using the `content-job` skill. The jobs run in the repository `FibonAdithya/content-pipeline` and open pull requests. This page lists the owner's steps. Run them after the pipeline is merged to `main` in that repository, so that jobs find the briefs.

## One-time setup

1. Give the executor's GitHub token access to `FibonAdithya/content-pipeline`.
2. Add `FibonAdithya/content-pipeline` to the `repos` list in `broker.json`. `add_repo` is not needed, because `run_task` clones from GitHub inside the container.
3. Protect the default branch of the repository if the GitHub plan allows it for private repositories. Without protection, the only guard against a push to `main` is the executor's instruction.
4. On the droplet, as the `hermes` user, pull this repository and install the skill:

   ```bash
   git -C ~/hermes-harness pull
   bash ~/hermes-harness/droplet/install-content-job.sh
   ```

5. Restart the gateway:

   ```bash
   systemctl --user restart hermes-gateway
   ```

   The restart is required for step 2. The broker runs as an MCP child process of `hermes-gateway` and reads `broker.json` once, on its first tool call (`_config()` in `broker/hermes_broker/server.py`), so a running broker does not see a new repo until the gateway restarts. Whether a new skill alone needs the restart is not verified.

## Use

1. Ask Hermes for interview questions for a post, with a skeleton or a topic. Hermes picks a slug and tells you.
2. Hermes starts the questions run (20 minutes), reports the task id and posts the questions. Answer either on the GitHub pull request, or by replying to Hermes: start each answer on a new line with its question number (`1.`), and put its source after the label `Source:`.
3. Merge the pull request and tell Hermes. Hermes starts the draft run (90 minutes). A reply you sent Hermes goes into the draft prompt unchanged, and `scripts/answers.py` in the content-pipeline repository copies each numbered answer into `interview.md`. A reply without question numbers, or one that numbers a question twice, is not used; the draft pull request lists anything that was not placed.
4. A final message that starts with `REFUSED:` is relayed as it is.

Each run needs a live grant for the box. Hermes calls `request_access` when there is none.
