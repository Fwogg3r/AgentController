"""agentlocal: run a local (or any OpenAI-compatible) model as a coding agent, scoped by a tier profile.

Layers (see docs/DECISIONS.md): model facts, tier policy, repo settings, environment. A run with the
"staged" runner: plan (model splits the idea into stages) -> per stage: spec (model writes exact edits)
-> edit (aider applies them, thinking off) -> scope guard -> test -> fix -> commit.
"""
