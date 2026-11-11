# Review: tiered router and staged pipeline for local coding agents

## Headline findings

1. **Your failed run was over budget before thinking started.** At 64k context, 51k input plus a 16k output cap is 67k. The fix is not a better prompt. The input budget must be derived as `context − max_output − reserve`, not set as a fraction, and every call needs a pre-flight check.
2. **Your evidence already contains the design.** Exact-code briefs work. Idea-level briefs fail because thinking and edit generation compete for one reply inside aider. So the pipeline's real job is to have the same model turn an idea into exact-code briefs in a separate call, then have aider apply them with thinking off. Everything else is secondary.
3. **The config path is mangled.** `Z:sharedgentcontroller	iers.toml` is what you get when `\a` and `\t` are interpreted as escapes. This bug will recur in TOML basic strings, where `\a` is invalid and `\t` is a tab. Document literal strings (`'Z:\shared\...'`) or forward slashes, and test path loading on Windows.

## Q1: Is self-decomposition by a ~27B model reliable enough?

It is usable if the code does the arithmetic and the model does the judgment. Models this size reason about code structure acceptably but cannot estimate token counts. They also tend to under-list the files a stage needs.

**Bypass aider for planning.** Call LM Studio's OpenAI-compatible endpoint directly. You then control `max_tokens`, can stream and display the reasoning, abort runaway thinking, and avoid paying aider's ~3k-token edit-format prompt for a task that edits nothing.

**Make planning robust with these rules:**
- **Annotate the repo map with per-file token costs** computed by code (e.g. `src/x.py  2.1k tok`). State the budget explicitly: "each stage's edit+read files must total ≤ N tokens."
- **Have the model list the done-criteria first**, then stages. Validation checks that every criterion maps to at least one stage.
- **Use a schema with no free-floating prose:**
  ```json
  {"criteria": ["..."],
   "stages": [{"id": 1, "goal": "...", "edit_files": ["..."], "create_files": ["..."],
     "read_files": ["..."], "acceptance": "pytest tests/test_x.py::test_y",
     "covers": [0, 2], "depends_on": []}]}
  ```
- **Validate in code:**
  - Edit and read files exist; created files don't.
  - `len(edit_files) ≤ max_edit_files`.
  - Code-computed tokens fit the budget.
  - The acceptance command matches an allowlisted pattern.
  - The dependency graph is acyclic.
  
  Return the specific errors to the model, at most 2 rounds, then escalate to the human.
- **Constrain stage shape:** each stage must leave the test suite green, and vertical slices beat layer-by-layer.
- **JSON reliability:** LM Studio supports `response_format: json_schema`, but grammar-constrained decoding can interact badly with a thinking phase. The safest pattern is to let the model think, extract the last JSON block, and validate. If that fails, make one no-think "reformat this as JSON" call. Measure both approaches in `probe` (see MVP).

## Q2: Aider architect mode or your own two-pass loop?

**Build your own loop.** Architect mode with the same model as editor still puts the architect's thinking and its proposal in one capped reply. It is also opaque to agentdash and gives you no hook to enforce a thinking budget or abort.

**Recommended per-stage flow:**
1. **Spec pass (direct API, thinking on, budgeted).** Input: stage goal, the stage's files in full, and prior-stage interface summaries. Output: a concrete change spec with exact snippets or near-exact code per file. This manufactures the exact-code brief that already works.
2. **Edit pass (aider, thinking off).**
   ```
   aider --message-file spec.md <edit_files> --read <read_files> \
     --edit-format diff --yes-always --no-suggest-shell-commands --no-auto-test
   ```
   Add a `.aider.model.settings.yml` entry with `reasoning_tag: think`, plus `extra_params` for sampling.
3. **Test and fix (agentlocal runs the fixed command, not the model).** On failure, re-enter the spec pass with the failing output, not aider directly.

**Thinking control for Qwen through LM Studio:**
- `/no_think` works only on Qwen variants whose chat template implements the soft switch. Separate Instruct/Thinking releases ignore it.
- `enable_thinking`-style template kwargs may not pass through LM Studio's endpoint.
- Treat all of these as unverified quirks. Measure them per model by sending the toggle and counting reasoning tokens.
- For a hard budget, stream the response and abort when reasoning exceeds the tier limit. Then retry once with no-think plus "answer from this reasoning: <truncated reasoning>".
- Reasoning may arrive in `reasoning_content` (if LM Studio's separation setting is on) or inline `<think>` tags. Handle both.
- True budget forcing (injecting `</think>` through the raw completions endpoint) is worth trying later.

## Q3: Are the tier fields right?

**Restructure into four layers rather than one bag of knobs:**

| Layer | Owns |
|---|---|
| **Model** (facts) | endpoint, `context_tokens`, `max_output`, `chars_per_token` (measured), thinking toggle, reasoning field/tag, sampling |
| **Tier** (policy) | runner, decompose, file limits, thinking budgets per phase, autonomy, gates, fix attempts |
| **Repo** (`agentcontroller.toml`) | test commands, acceptance allowlist, protected paths |
| **Environment** (runtime) | sandbox present or not. Effective permissions = `min(tier, environment)` |

`context_tokens` and `max_output` describe the model and hardware, not policy. Move them to the model layer.

**Over-engineered:**
- `stage_input_budget` as a fraction: derive it from the model layer instead.
- `context_mode` with three options: outlines from a repo map lose exactly the details edits need, and they go stale. The MVP should send stage files whole and keep stages small.
- Four permission levels plus three separate gate flags: collapse into `autonomy` and a `gates` list.
- `decompose = if_over_budget`: you can't know a task's size before planning it. Use `always` or `never`.

**Missing:**
- `runner` (`staged` | `aider-single` | `passthrough`), which is what lets top tiers skip the machinery.
- `max_stages`.
- Separate thinking budgets for plan, spec, and fix.
- `allow_new_files`.
- Per-call timeouts. At 57 tok/s, a full 16k reply takes about 4.7 minutes before prompt processing.
- Calibrated `chars_per_token`. chars/3.5 underestimates dense code, so calibrate from `usage.prompt_tokens` and add a 15% margin.

## Q4: Failure modes not yet covered

- **Context overflow:** pre-flight every call (input + max_output + reserve ≤ context). If it doesn't fit, split the stage. Never truncate silently.
- **Truncated edits:** `finish_reason == "length"` during the edit pass means a malformed edit. Discard it and re-spec into smaller chunks. Prefer `diff` over `whole`.
- **Plan drift:** after each stage, make one cheap no-think call with the remaining plan, the `git diff --stat`, and the summary. The model replies `continue` or returns amended remaining stages, which go through the same validator. Stages already done are never rewritten.
- **Summaries losing details:** make summaries mostly code-derived. Include changed files, new or changed public signatures (from AST diff of touched files), and test results, plus the model's 3–5 lines on intent. Later stages need interfaces, not narrative.
- **Stale outlines:** avoided by using whole files. If you add outlines later, regenerate them from the current worktree each stage, cached by file hash.
- **Edits outside the stage:** with `--yes-always`, aider will add files the model asks for. After the edit pass, diff changed paths against `edit_files ∪ create_files`. Anything else gets reverted or gated.
- **Weakened tests:** treat test-file changes outside the stage, and deleted assertions, as gate events.
- **Fix-loop thrash:** hash the failure signature (test id + exception type) and stop when it repeats.
- **Slow tests:** run the stage's acceptance command per stage and the full suite only at the end. Enforce a hard timeout.
- **Resume divergence:** commit after each passing stage and record the SHA in progress.json. On resume, verify HEAD matches or refuse to continue.
- **Sandbox networking:** WSL2 or Windows Sandbox must reach LM Studio on the host. That needs the "serve on local network" setting or WSL mirrored networking, plus a firewall rule. Windows Sandbox also discards state on close, so the run directory must live in the mapped worktree or on the host.
- **Supervisor conflict:** don't launch agentlocal from Claude Code's auto mode. The owner launches it from a terminal. The model never chooses shell commands; agentlocal runs the configured test command. Tests still execute model-written code, so unattended runs need the sandbox. Until then, gate test runs.

## Q5: What would slow down a top-tier model?

- A mandatory plan pass, JSON schemas, per-stage tests, summaries replacing history, outlines, and aider's edit formats. Strong models do better with full history and their own tools.
- **Fix:** the router is a thin dispatcher, and the staged pipeline is one runner among several. Top tier means `runner = "passthrough"`: hand the idea to Claude Code or aider in a single shot, with no gates and only event logging.
- **Requirement conflict:** requirement 1's "scope to the lowest-end model" conflicts with requirement 2. Resolve it per run: each run is scoped to the model doing that run, not to the weakest model installed.

## Recommended MVP

### File layout

```
src/agentcontroller/local/
  cli.py          # agentlocal entry point
  config.py       # load + merge model/tier/repo layers; AGENTCONTROLLER_TIERS env, tiers_path key
  budget.py       # token estimate, calibration, preflight
  llm.py          # streaming OpenAI-compatible client, reasoning split, budget abort
  plan.py         # plan prompt, validation, split loop
  stage.py        # spec pass -> aider edit -> test -> fix
  scope.py        # diff guard, test-tamper check
  events.py       # JSONL writer (agentdash tails it)
  approvals.py    # Approver protocol: DashboardApprover (default), CommandHookApprover
  prompts/{plan,spec,fix,replan}.md
  schemas/plan.schema.json
examples/tiers.example.toml
tests/local/      # validator, budget, scope tests (no model needed)
```

Each run writes to `.agentcontroller/runs/<id>/` (gitignored): `plan.json`, `progress.json`, `events.jsonl`, and `stages/NN/{spec.md, reasoning.txt, aider.log, test.log}`. Keep reasoning in files and put only token counts in events, so the dashboard log stays light.

### CLI

```
agentlocal probe  --model qwen-27b        # tok/s, chars/token, no-think works?, JSON compliance -> writes calibration
agentlocal plan   "idea" --model qwen-27b # plan.json + approval gate
agentlocal run    <run_id | "idea">       # execute stages
agentlocal resume <run_id>
agentlocal status <run_id>
```

### Tier format

```toml
[models."qwen-27b"]
tier = "small"
endpoint = "http://localhost:1234/v1"
context_tokens = 65536
max_output = 16384
chars_per_token = 3.2            # written by probe
no_think = { method = "suffix", value = "/no_think" }   # verified by probe
reasoning = { field = "reasoning_content", tag = "think" }
sampling = { temperature = 0.6, top_p = 0.95 }

[tiers.small]
runner = "staged"
decompose = "always"
max_stages = 8
max_edit_files = 2
allow_new_files = true
thinking = { plan = 8000, spec = 6000, fix = 4000, edit = 0 }
edit_format = "diff"
max_fix_attempts = 2
autonomy = "edit"                # capped by environment
gates = ["plan", "stage", "tests"]

[tiers.top]
runner = "passthrough"
decompose = "never"
gates = []
```

Repo-layer settings in `agentcontroller.toml`, under `[local]`:
- `tiers_path` (literal string)
- `test_cmd = "uv run pytest -q"`
- `acceptance_allow = ["uv run pytest *"]`
- `protected = ["tests/**"]`

Event schema per line: `{ts, run_id, stage, kind, tokens_in, tokens_out, reasoning_tokens, context_pct, detail}`. This covers the dashboard requirements (context gauge, thinking vs answer, edits, tests) without a new protocol. The agent-kind icon is just a `kind` field on the run record.

## Defer

- Outlines and `context_mode`.
- Automated sandbox provisioning and the `shell`/`full` autonomy levels.
- Discord notifier. The generic command hook covers it.
- Automatic tier selection from task heuristics.
- Raw-completion budget forcing.
- Replan beyond the single continue/amend check.
- Roadmap notes and rich reports.
- Multi-model chains.

Build `probe`, `plan` with the validator, and one staged run on a real idea first. Your clearest signal is whether the spec pass reliably produces briefs as good as the hand-written exact-code briefs.