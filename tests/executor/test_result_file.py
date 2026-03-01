import json

import pytest

from wowkmang.executor.result_file import (
    CommitInfo,
    CommentInfo,
    QuestionInfo,
    TaskOutput,
    parse_result_file,
)


class TestParseResultFile:
    def test_parses_full_result(self):
        raw = json.dumps(
            {
                "commit": {
                    "title": "Fix bug",
                    "description": "Fixes a critical bug",
                    "branch_name": "fix-bug",
                },
                "comment": {"message": "Done!"},
                "questions": [{"message": "Which approach?", "choices": ["A", "B"]}],
            }
        )
        result = parse_result_file(raw)

        assert result.commit.title == "Fix bug"
        assert result.commit.description == "Fixes a critical bug"
        assert result.commit.branch_name == "fix-bug"
        assert result.comment.message == "Done!"
        assert len(result.questions) == 1
        assert result.questions[0].message == "Which approach?"
        assert result.questions[0].choices == ["A", "B"]

    def test_parses_commit_only(self):
        raw = json.dumps(
            {
                "commit": {"title": "Add feature", "branch_name": "add-feature"},
            }
        )
        result = parse_result_file(raw)

        assert result.commit is not None
        assert result.comment is None
        assert result.questions == []

    def test_parses_empty_object(self):
        raw = json.dumps({})
        result = parse_result_file(raw)

        assert result.commit is None
        assert result.comment is None
        assert result.questions == []

    def test_parses_questions_without_choices(self):
        raw = json.dumps(
            {
                "questions": [{"message": "What should I do?"}],
            }
        )
        result = parse_result_file(raw)

        assert len(result.questions) == 1
        assert result.questions[0].choices == []

    def test_invalid_json_raises(self):
        with pytest.raises(json.JSONDecodeError):
            parse_result_file("not json")

    def test_comment_only(self):
        raw = json.dumps({"comment": {"message": "I looked into this."}})
        result = parse_result_file(raw)

        assert result.commit is None
        assert result.comment.message == "I looked into this."


class TestTaskOutput:
    def test_defaults(self):
        output = TaskOutput()
        assert output.commit is None
        assert output.comment is None
        assert output.questions == []

    def test_with_commit(self):
        output = TaskOutput(commit=CommitInfo(title="msg", branch_name="br"))
        assert output.commit.title == "msg"


class TestCommitInfo:
    def test_fields(self):
        ci = CommitInfo(title="Fix", branch_name="fix-it")
        assert ci.title == "Fix"
        assert ci.branch_name == "fix-it"


class TestQuestionInfo:
    def test_defaults(self):
        q = QuestionInfo(message="Why?")
        assert q.message == "Why?"
        assert q.choices == []

    def test_with_choices(self):
        q = QuestionInfo(message="Pick one", choices=["A", "B", "C"])
        assert len(q.choices) == 3
