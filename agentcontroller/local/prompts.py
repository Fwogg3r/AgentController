"""Prompts for the staged runner. The user's instruction file (tier.instructions) is prepended to every system prompt."""

SYSTEM = """You are a careful senior software engineer working on a code repository through a supervisor program.
The program does all token counting, file handling, git and tests; you do the engineering judgment.
Be concrete and brief. Never invent files, functions or APIs: use only what you have been shown."""

PLAN = """# Task (a high-level idea from the owner)
{idea}

# Your limits (enforced by the program)
- At most {max_stages} stages; each stage edits at most {max_edit_files} existing files{new_files}.
- Each stage's files (edit + read) must total at most {stage_budget} tokens. File sizes are listed below.
- Each stage must leave the test suite passing and should be a vertical slice (code + its test together).
- Acceptance commands must match one of: {allow}

# Repository files (path, estimated size)
{repo_map}

# What to do
Think about which files are involved, then answer with ONLY one JSON object, no prose after it:
{{"criteria": ["done-criterion 1", "..."],
  "stages": [{{"id": 1, "goal": "one or two sentences", "edit_files": ["path"], "create_files": [],
              "read_files": ["path"], "acceptance": "command", "covers": [0], "depends_on": []}}]}}
"criteria" lists what must be true when the whole idea is done; every criterion must be covered by a stage."""

REPAIR = """Your plan was rejected by the validator:
{errors}
Return the corrected plan as ONLY one JSON object in the same format."""

SPEC = """# Overall idea
{idea}

# This stage ({sid} of {n}): {goal}
Files you may edit: {edit_files}
Files you may create: {create_files}
Acceptance check: {acceptance}

# What earlier stages did
{summaries}

# Current file contents
{files}

# What to do
Write the exact change for this stage as a spec another program will apply literally:
for each file, the exact code to add or replace, with enough surrounding original lines to locate it
(copy them exactly). Include the test(s) that prove the stage. No explanations beyond one line per change.
Only touch the files listed above."""

FIX = """The stage's acceptance check failed after applying your spec.

# Check output (last part)
{output}

# Current file contents
{files}

Write a corrected spec in the same format: exact code changes that make the check pass.
Only touch the files of this stage: {edit_files} {create_files}"""

SUMMARY = """In 3-5 lines, say what this stage changed and which public names (functions, classes,
parameters) later stages can rely on. Diff stat:
{diffstat}"""

REPLAN = """# Overall idea
{idea}

Done-criteria (by index): {criteria}

# Stages finished and committed
{done}

# This stage failed its acceptance check after every fix attempt; its changes were rolled back
{failed}

# Failure (last part of the check output)
{failure}

# Remaining planned stages after it (JSON)
{remaining}

# Diff stat of the finished stages
{diffstat}

# Your limits (enforced by the program)
- At most {max_stages} stages; each stage edits at most {max_edit_files} existing files.
- Each stage's files (edit + read) must total at most {stage_budget} tokens.
- Acceptance commands must match one of: {allow}

# Repository files (path, estimated size)
{repo_map}

# What to do
Decide whether a different split would succeed, e.g. a smaller first step, a missing prerequisite, or a file
that was left out. If the plan cannot be improved, answer exactly: continue
Otherwise answer with ONLY one JSON object {{"stages": [...]}} that replaces the failed stage and all stages after
it (same stage format as before, new ids not used by finished stages; "covers" refers to the criteria indexes
above, and every criterion not covered by a finished stage must be covered)."""
