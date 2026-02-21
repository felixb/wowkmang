import json
import logging
import re

import yaml
from pydantic import BaseModel

from wowkmang.api.config import ProjectConfig
from wowkmang.executor.docker_runner import DockerRunner
from wowkmang.taskqueue.models import Task

logger = logging.getLogger(__name__)

MAX_DIFF_CHARS = 10000


class PRMetadata(BaseModel):
    title: str
    branch: str  # includes wowkmang/ prefix
    description: str


class SummaryGenerator:
    def __init__(self, docker_runner: DockerRunner):
        self.docker_runner = docker_runner

    def generate(
        self,
        task: Task,
        diff: str,
        hook_output: str | None = None,
        project: ProjectConfig | None = None,
        work_dir: str | None = None,
        project_volume: str | None = None,
    ) -> PRMetadata:
        """Generate PR metadata by continuing the Claude Code session with haiku."""
        prompt = self._build_prompt(task, diff, hook_output)

        try:
            result = self.docker_runner.run_claude_code(
                work_dir=work_dir or "/workspace",
                project_volume=project_volume or "",
                task_prompt=prompt,
                model="haiku",
                project=project or ProjectConfig(name="_summary", repo=""),
                timeout_minutes=5,
                continue_session=True,
                output_format="json",
            )
            metadata = _parse_response(result.logs)
            return PRMetadata(
                title=metadata["title"],
                branch=f"wowkmang/{metadata['branch']}",
                description=metadata["description"],
            )
        except Exception as e:
            logger.warning("Summary generation failed (%s), using fallback metadata", e)
            return _fallback_metadata(task)

    @staticmethod
    def _build_prompt(task: Task, diff: str, hook_output: str | None) -> str:
        parts = [
            "Generate PR metadata for the following changes.",
            "",
            f"Task: {task.task}",
            f"Source: {task.source.type.value}",
        ]

        if task.source.issue_number:
            parts.append(f"Issue: #{task.source.issue_number}")
        if task.source.pr_number:
            parts.append(f"PR: #{task.source.pr_number}")

        truncated_diff = diff[:MAX_DIFF_CHARS]
        parts.extend(["", "Git diff:", "```", truncated_diff, "```", ""])

        if hook_output:
            parts.append(f"Post-task hooks FAILED with output:\n{hook_output}")
        else:
            parts.append("All post-task hooks passed.")

        parts.extend(
            [
                "",
                "Respond in YAML format:",
                "```yaml",
                "title: <concise PR title>",
                "branch: <short-kebab-case-name, no prefix>",
                "description: |",
                '  <PR description, include "Closes #N" if an issue number is available>',
                "```",
            ]
        )

        return "\n".join(parts)


def _fallback_metadata(task) -> PRMetadata:
    """Generate minimal PR metadata without calling Claude."""
    title = task.task[:72].strip()
    branch = re.sub(r"[^a-z0-9-]", "-", title.lower())[:50].strip("-")
    branch = re.sub(r"-+", "-", branch)
    return PRMetadata(
        title=title,
        branch=f"wowkmang/{branch}",
        description=f"Automated changes for: {task.task}",
    )


def _parse_response(logs: str) -> dict:
    """Parse Claude Code JSON output to extract YAML metadata."""
    # Claude Code --output-format json wraps the result in a JSON object
    try:
        data = json.loads(logs)
        text = data.get("result", logs)
    except (json.JSONDecodeError, TypeError):
        text = logs

    raw_yaml = _extract_yaml(text)
    metadata = yaml.safe_load(raw_yaml)

    if not isinstance(metadata, dict) or not all(
        k in metadata for k in ("title", "branch", "description")
    ):
        raise ValueError("Invalid YAML response: missing required fields")

    return metadata


def _extract_yaml(text: str) -> str:
    """Extract YAML block from model response text."""
    match = re.search(r"```(?:yaml)?\s*\n(.*?)```", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    # Fall back to treating the whole response as YAML
    return text.strip()
