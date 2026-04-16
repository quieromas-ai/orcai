"""Tests for platform message parsers and get_parser factory."""

import pytest

from router import ProjectConfig
from router.platforms import get_parser
from router.platforms.azure_devops import AzureDevOpsMessageParser
from router.platforms.github import GitHubMessageParser
from router.platforms.slack import SlackMessageParser

# ---------------------------------------------------------------------------
# extract_message_text
# ---------------------------------------------------------------------------


def test_github_extract_message_text_from_top_level() -> None:
    parser = GitHubMessageParser()
    msg = {"text": "  Hello world  "}
    assert parser.extract_message_text(msg) == "Hello world"


def test_github_extract_message_text_from_attachment_when_text_empty() -> None:
    parser = GitHubMessageParser()
    msg = {
        "text": "",
        "attachments": [
            {"pretext": "PR opened", "text": "Description of the PR"},
        ],
    }
    assert parser.extract_message_text(msg) == "PR opened\nDescription of the PR"


def test_github_extract_message_text_from_attachment_pretext_only() -> None:
    parser = GitHubMessageParser()
    msg = {"text": "", "attachments": [{"pretext": "Issue assigned"}]}
    assert parser.extract_message_text(msg) == "Issue assigned"


def test_github_extract_message_text_empty_no_attachment() -> None:
    parser = GitHubMessageParser()
    msg = {"text": ""}
    assert parser.extract_message_text(msg) == ""


def test_github_extract_message_text_empty_attachments_list() -> None:
    parser = GitHubMessageParser()
    msg = {"text": "", "attachments": []}
    assert parser.extract_message_text(msg) == ""


def test_azure_devops_extract_message_text_same_as_github() -> None:
    parser = AzureDevOpsMessageParser()
    msg = {"text": "  ADO update  "}
    assert parser.extract_message_text(msg) == "ADO update"


def test_azure_devops_extract_message_text_from_attachment() -> None:
    parser = AzureDevOpsMessageParser()
    msg = {
        "text": "",
        "attachments": [{"pretext": "Work item updated", "text": "Details"}],
    }
    assert parser.extract_message_text(msg) == "Work item updated\nDetails"


def test_azure_devops_combines_text_with_attachment_title() -> None:
    # Reproduces the real failure: Azure Boards puts the event summary in
    # `text` and the work item title in `attachments[0].title`.  Both must
    # reach the engineer so it can identify the task number.
    parser = AzureDevOpsMessageParser()
    msg = {
        "text": "Tomasz Węgliński updated the title of a Task.",
        "attachments": [
            {
                "title": (
                    "Task 517: [Agent] Test E2E login using Google social auth"
                    " in example-demo app"
                ),
                "title_link": "https://dev.azure.com/org/project/_workitems/edit/517",
                "fallback": (
                    "Task 517: [Agent] Test E2E login using Google social auth"
                    " in example-demo app"
                ),
                "text": "",
            }
        ],
    }
    result = parser.extract_message_text(msg)
    assert "Tomasz Węgliński updated the title of a Task." in result
    assert "Task 517" in result
    assert "Google social auth" in result


def test_azure_devops_uses_fallback_when_no_title() -> None:
    parser = AzureDevOpsMessageParser()
    msg = {
        "text": "Work item updated.",
        "attachments": [
            {
                "fallback": "Task 42: Some task name",
                "text": "",
            }
        ],
    }
    result = parser.extract_message_text(msg)
    assert "Work item updated." in result
    assert "Task 42" in result


def test_azure_devops_text_only_no_attachments() -> None:
    parser = AzureDevOpsMessageParser()
    msg = {"text": "No attachments here"}
    assert parser.extract_message_text(msg) == "No attachments here"


def test_azure_devops_attachment_title_only_no_top_text() -> None:
    parser = AzureDevOpsMessageParser()
    msg = {
        "text": "",
        "attachments": [{"title": "Task 99: standalone", "text": ""}],
    }
    assert parser.extract_message_text(msg) == "Task 99: standalone"


# ---------------------------------------------------------------------------
# should_forward
# ---------------------------------------------------------------------------


def test_github_should_forward_default_true() -> None:
    parser = GitHubMessageParser()
    project = ProjectConfig(
        name="p", workspace="/w", channels=[], platform="github"
    )
    assert parser.should_forward({"text": "x"}, project) is True


def test_azure_devops_should_forward_default_true() -> None:
    parser = AzureDevOpsMessageParser()
    project = ProjectConfig(
        name="p", workspace="/w", channels=[], platform="azure_devops"
    )
    assert parser.should_forward({"text": "x"}, project) is True


# ---------------------------------------------------------------------------
# get_parser
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# SlackMessageParser
# ---------------------------------------------------------------------------


def test_slack_extract_message_text_returns_text() -> None:
    parser = SlackMessageParser()
    msg = {"text": "Hello world"}
    assert parser.extract_message_text(msg) == "Hello world"


def test_slack_extract_message_text_strips_whitespace() -> None:
    parser = SlackMessageParser()
    msg = {"text": "  trimmed  "}
    assert parser.extract_message_text(msg) == "trimmed"


def test_slack_extract_message_text_empty() -> None:
    parser = SlackMessageParser()
    assert parser.extract_message_text({}) == ""


def test_slack_should_forward_always_true() -> None:
    parser = SlackMessageParser()
    project = ProjectConfig(name="p", workspace="/w", channels=[], platform="slack")
    assert parser.should_forward({"text": "x"}, project) is True


def test_slack_extract_ticket_id_returns_thread_ts_when_present() -> None:
    parser = SlackMessageParser()
    msg = {"thread_ts": "1234567890.000001", "ts": "1234567891.000001"}
    assert parser.extract_ticket_id(msg) == "1234567890.000001"


def test_slack_extract_ticket_id_falls_back_to_ts() -> None:
    parser = SlackMessageParser()
    msg = {"ts": "1234567890.000001"}
    assert parser.extract_ticket_id(msg) == "1234567890.000001"


def test_slack_extract_ticket_id_returns_none_when_no_ts() -> None:
    parser = SlackMessageParser()
    assert parser.extract_ticket_id({}) is None


def test_get_parser_slack_returns_slack_parser() -> None:
    parser = get_parser("slack")
    assert isinstance(parser, SlackMessageParser)


# ---------------------------------------------------------------------------
# get_parser
# ---------------------------------------------------------------------------


def test_get_parser_github_returns_github_parser() -> None:
    parser = get_parser("github")
    assert isinstance(parser, GitHubMessageParser)


def test_get_parser_azure_devops_returns_azure_parser() -> None:
    parser = get_parser("azure_devops")
    assert isinstance(parser, AzureDevOpsMessageParser)


def test_get_parser_normalizes_platform_name() -> None:
    parser = get_parser("  GITHUB  ")
    assert isinstance(parser, GitHubMessageParser)


def test_get_parser_unknown_platform_raises() -> None:
    with pytest.raises(ValueError, match="Unknown platform"):
        get_parser("unknown_platform")


def test_get_parser_empty_platform_raises() -> None:
    with pytest.raises(ValueError, match="cannot be empty"):
        get_parser("")


# ---------------------------------------------------------------------------
# extract_ticket_id
# ---------------------------------------------------------------------------


def test_github_extract_ticket_id_from_issues_url() -> None:
    parser = GitHubMessageParser()
    msg = {"attachments": [{"title_link": "https://github.com/org/repo/issues/42"}]}
    assert parser.extract_ticket_id(msg) == "42"


def test_github_extract_ticket_id_from_pull_url() -> None:
    parser = GitHubMessageParser()
    msg = {"attachments": [{"title_link": "https://github.com/org/repo/pull/17"}]}
    assert parser.extract_ticket_id(msg) == "17"


def test_github_extract_ticket_id_fallback_to_text() -> None:
    parser = GitHubMessageParser()
    msg = {"text": "PR #99 was merged", "attachments": []}
    assert parser.extract_ticket_id(msg) == "99"


def test_github_extract_ticket_id_no_match_returns_none() -> None:
    parser = GitHubMessageParser()
    msg = {"text": "Hello world", "attachments": []}
    assert parser.extract_ticket_id(msg) is None


def test_azure_devops_extract_ticket_id_from_url() -> None:
    parser = AzureDevOpsMessageParser()
    msg = {
        "attachments": [
            {"title_link": "https://dev.azure.com/org/project/_workitems/edit/517"}
        ]
    }
    assert parser.extract_ticket_id(msg) == "517"


def test_azure_devops_extract_ticket_id_from_title() -> None:
    parser = AzureDevOpsMessageParser()
    msg = {
        "attachments": [
            {"title": "Task 517: do something", "title_link": ""}
        ]
    }
    assert parser.extract_ticket_id(msg) == "517"


def test_azure_devops_extract_ticket_id_from_title_with_hash() -> None:
    parser = AzureDevOpsMessageParser()
    msg = {
        "attachments": [
            {"title": "Task #519 ([agent-task] Test registration)", "title_link": ""}
        ]
    }
    assert parser.extract_ticket_id(msg) == "519"


def test_azure_devops_extract_ticket_id_fallback_to_text() -> None:
    parser = AzureDevOpsMessageParser()
    msg = {"text": "Task #524 (Add API endpoint) updated by Someone", "attachments": []}
    assert parser.extract_ticket_id(msg) == "524"


def test_azure_devops_extract_ticket_id_fallback_to_text_no_hash() -> None:
    parser = AzureDevOpsMessageParser()
    msg = {"text": "Task 524 (Add API endpoint) updated by Someone"}
    assert parser.extract_ticket_id(msg) == "524"


def test_azure_devops_extract_ticket_id_no_match_returns_none() -> None:
    parser = AzureDevOpsMessageParser()
    msg = {"text": "Hello world", "attachments": []}
    assert parser.extract_ticket_id(msg) is None


def test_azure_devops_extract_ticket_id_from_query_param_url() -> None:
    # ADO work item URLs use query params (wi.aspx?...&id=537), not path segments.
    # The /(\d+)$ pattern fails; [?&]id=(\d+) must pick it up.
    parser = AzureDevOpsMessageParser()
    msg = {
        "attachments": [
            {
                "title_link": (
                    "https://dev.azure.com/thisexample/web/wi.aspx"
                    "?pcguid=c63462e9-f3c6-4b9f-9e15-9c67586cafed&id=537"
                )
            }
        ]
    }
    assert parser.extract_ticket_id(msg) == "537"


def test_azure_devops_extract_ticket_id_copy_does_not_match_source_task() -> None:
    # When task #537 is a copy of #532, ADO sends a notification whose text
    # mentions both IDs (source first).  The title_link must be used to extract
    # the correct destination ID (537), not the first regex hit (532).
    parser = AzureDevOpsMessageParser()
    msg = {
        "text": "Task #532 was copied to Task #537",
        "attachments": [
            {
                "title": "Task #537: Implement feature X",
                "title_link": (
                    "https://dev.azure.com/thisexample/web/wi.aspx"
                    "?pcguid=c63462e9-f3c6-4b9f-9e15-9c67586cafed&id=537"
                ),
            }
        ],
    }
    assert parser.extract_ticket_id(msg) == "537"
