# Wowkmang

Wowkmang orchestrates claude code tasks

## Basics

Language: python
Test framework: pytest, run `uv run pytest`
Formatting: black (python), mdformat (markdown), yamlfix (yaml), jq (json)
Style: imports at top of file, never inside functions
Deployable: docker
Claude models: only specify short name like opus, sonnet, haiku

## Rules

**Rule: Ask the user:** Unless explicitly asked to run unattended: When unclear, ask the user with AskUserTool to clarify questions.
**Rule: Write tests:** Always write tests for new code. When fixing bugs, write test first reproducing the issue.
