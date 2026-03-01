import json
import logging

from pydantic import BaseModel

logger = logging.getLogger(__name__)

RESULT_FILE_PATH = "repo/.claude-result.json"


class CommitInfo(BaseModel):
    title: str
    description: str | None = None
    branch_name: str


class CommentInfo(BaseModel):
    message: str


class QuestionInfo(BaseModel):
    message: str
    choices: list[str] = []


class TaskOutput(BaseModel):
    commit: CommitInfo | None = None
    comment: CommentInfo | None = None
    questions: list[QuestionInfo] = []


def parse_result_file(raw: str) -> TaskOutput:
    """Parse .claude-result.json content into a TaskOutput model."""
    data = json.loads(raw)
    return TaskOutput(**data)
