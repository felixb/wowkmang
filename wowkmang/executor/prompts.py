from wowkmang.api.config import ProjectConfig
from wowkmang.taskqueue.models import Task


def issue_task_prompt(title: str, body: str) -> str:
    """Build the task description for a GitHub issue."""
    return f"Fix the issue:\n\nTitle: {title}\n\n{body or ''}"


def pr_task_prompt(title: str, body: str) -> str:
    """Build the task description for a GitHub PR review."""
    return f"Address the review on this PR:\n\nTitle: {title}\n\n{body or ''}"


RESULT_FILE_SCHEMA = """\
Write a JSON file called `.claude-result.json` in the repo root when you are done.
All fields are optional. Schema:

```json
{
  "commit": {
    "title": "concise PR title",
    "description": "<PR description, include "Closes #N" if an issue number is available>",
    "branch_name": "short-kebab-case-branch-name"
  },
  "comment": {
    "message": "markdown comment to post on the issue/PR"
  },
  "questions": [
    {"message": "question text", "choices": ["A", "B"]}
  ]
}
```

Rules for `.claude-result.json`:
- `commit`: must be included when any changes were made. `branch_name` should be short kebab-case without any prefix.
- `comment`: include when you want to post a comment on the source issue/PR.
- `questions`: include ONLY when explicitly allowed and you are blocked and need clarification.
- You may include both `commit` and `questions` (commit first, then ask questions).
"""

QUESTIONS_INSTRUCTIONS = """\
You ARE allowed to ask questions. If you are blocked or need clarification,
include a `questions` array in `.claude-result.json`. Each question should have
a `message` (the question text) and optionally `choices` (a list of options).
"""

NO_QUESTIONS_INSTRUCTIONS = """\
You are NOT allowed to ask questions. Do your best with the information provided.
"""


def build_task_prompt(
    task: Task,
    project: ProjectConfig,
    comments: str | None = None,
) -> str:
    """Build the full prompt for a Claude Code task run."""
    parts = [
        "You are running unattended in a CI-like environment. There is no human present.",
        "You MUST NOT use the `AskUserQuestion` tool — it will not work.",
        "",
        RESULT_FILE_SCHEMA,
        QUESTIONS_INSTRUCTIONS if task.allow_questions else NO_QUESTIONS_INSTRUCTIONS,
        "",
    ]

    # Source context
    if task.source.issue_number:
        parts.append(f"Source: GitHub issue #{task.source.issue_number}")
    elif task.source.pr_number:
        parts.append(f"Source: GitHub PR #{task.source.pr_number}")

    # Comments context
    if comments:
        parts.extend(
            [
                "",
                "## Issue/PR Comments",
                "",
                comments,
                "",
            ]
        )

    # The actual task
    parts.extend(
        [
            "",
            "## Task",
            "",
            task.task,
        ]
    )

    return "\n".join(parts)
