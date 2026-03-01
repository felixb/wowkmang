import re

from pydantic import BaseModel


class PRMetadata(BaseModel):
    title: str
    branch: str  # includes wowkmang/ prefix
    description: str


def fallback_metadata(task) -> PRMetadata:
    """Generate minimal PR metadata without calling Claude."""
    title = task.task[:72].strip()
    branch = re.sub(r"[^a-z0-9-]", "-", title.lower())[:50].strip("-")
    branch = re.sub(r"-+", "-", branch)
    return PRMetadata(
        title=title,
        branch=f"wowkmang/{branch}",
        description=f"Automated changes for: {task.task}",
    )
