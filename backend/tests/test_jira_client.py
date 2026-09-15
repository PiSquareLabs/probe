"""Jira client: ADF conversion, JQL escaping, auth errors and issue creation."""

import base64

import httpx
import pytest
import respx

from app.clients.jira import JiraClient, adf_to_text, escape_jql, text_to_adf
from app.errors import UpstreamError

from .conftest import JIRA_URL


# -- ADF -------------------------------------------------------------------
def test_plain_text_becomes_an_adf_document():
    doc = text_to_adf("Login fails on Safari.")
    assert doc["type"] == "doc" and doc["version"] == 1
    assert doc["content"][0]["content"][0]["text"] == "Login fails on Safari."


def test_blank_lines_split_paragraphs():
    doc = text_to_adf("First para.\n\nSecond para.")
    assert len(doc["content"]) == 2
    assert doc["content"][1]["content"][0]["text"] == "Second para."


def test_empty_description_is_still_valid_adf():
    doc = text_to_adf("")
    assert doc["type"] == "doc"
    assert doc["content"] == [{"type": "paragraph", "content": []}]


def test_adf_round_trips_back_to_text():
    assert adf_to_text(text_to_adf("Hello there")).strip() == "Hello there"


# -- JQL escaping ----------------------------------------------------------
def test_quotes_are_escaped():
    assert escape_jql('say "hi"') == 'say \\"hi\\"'


def test_backslashes_are_doubled_before_quotes_are_escaped():
    # A naive implementation would turn \" into \\" and break out of the literal.
    assert escape_jql('a\\"b') == 'a\\\\\\"b'


# -- client ----------------------------------------------------------------
@respx.mock
async def test_myself_sends_basic_auth(jira_creds):
    route = respx.get(f"{JIRA_URL}/rest/api/3/myself").mock(
        return_value=httpx.Response(
            200, json={"accountId": "5b10a", "displayName": "Probe Bot"}
        )
    )
    async with JiraClient(jira_creds) as client:
        me = await client.myself()

    assert me["displayName"] == "Probe Bot"
    expected = base64.b64encode(
        f"{jira_creds.account_email}:{jira_creds.api_token}".encode()
    ).decode()
    assert route.calls[0].request.headers["Authorization"] == f"Basic {expected}"


@respx.mock
async def test_401_explains_how_to_fix_it(jira_creds):
    respx.get(f"{JIRA_URL}/rest/api/3/myself").mock(return_value=httpx.Response(401))
    async with JiraClient(jira_creds) as client:
        with pytest.raises(UpstreamError) as exc:
            await client.myself()

    assert exc.value.upstream_status == 401
    assert "api-tokens" in (exc.value.hint or "")


@respx.mock
async def test_create_issue_builds_a_v3_payload(jira_creds):
    route = respx.post(f"{JIRA_URL}/rest/api/3/issue").mock(
        return_value=httpx.Response(201, json={"id": "10042", "key": "PROBE-42"})
    )
    async with JiraClient(jira_creds) as client:
        result = await client.create_issue(
            project_key="PROBE",
            summary="Checkout 500s",
            description="Stack trace attached.",
            issue_type="Bug",
            priority="High",
            labels=["from probe", "  "],
        )

    assert result == {
        "id": "10042",
        "key": "PROBE-42",
        "url": f"{JIRA_URL}/browse/PROBE-42",
    }
    fields = route.calls[0].request.read().decode()
    import json

    sent = json.loads(fields)["fields"]
    assert sent["project"] == {"key": "PROBE"}
    assert sent["issuetype"] == {"name": "Bug"}
    # v3 requires ADF, not a string.
    assert sent["description"]["type"] == "doc"
    # Whitespace in labels is not allowed by Jira, and blanks are dropped.
    assert sent["labels"] == ["from-probe"]


@respx.mock
async def test_create_issue_surfaces_field_errors(jira_creds):
    respx.post(f"{JIRA_URL}/rest/api/3/issue").mock(
        return_value=httpx.Response(
            400, json={"errors": {"issuetype": "valid issue type is required"}}
        )
    )
    async with JiraClient(jira_creds) as client:
        with pytest.raises(UpstreamError, match="valid issue type is required"):
            await client.create_issue(
                project_key="PROBE", summary="x", issue_type="Nope"
            )


@respx.mock
async def test_permission_check_reads_have_permission(jira_creds):
    respx.get(f"{JIRA_URL}/rest/api/3/mypermissions").mock(
        return_value=httpx.Response(
            200,
            json={
                "permissions": {
                    "BROWSE_PROJECTS": {"havePermission": True},
                    "CREATE_ISSUES": {"havePermission": False},
                }
            },
        )
    )
    async with JiraClient(jira_creds) as client:
        perms = await client.check_permissions("PROBE")

    assert perms == {"BROWSE_PROJECTS": True, "CREATE_ISSUES": False}


@respx.mock
async def test_issue_types_falls_back_to_the_legacy_createmeta(jira_creds):
    respx.get(
        f"{JIRA_URL}/rest/api/3/issue/createmeta/PROBE/issuetypes"
    ).mock(return_value=httpx.Response(404))
    respx.get(f"{JIRA_URL}/rest/api/3/issue/createmeta").mock(
        return_value=httpx.Response(
            200,
            json={
                "projects": [
                    {"issuetypes": [{"id": "10001", "name": "Bug", "subtask": False}]}
                ]
            },
        )
    )
    async with JiraClient(jira_creds) as client:
        types = await client.issue_types("PROBE")

    assert types == [
        {"id": "10001", "name": "Bug", "subtask": False, "description": None}
    ]


@respx.mock
async def test_search_falls_back_to_the_legacy_endpoint(jira_creds):
    respx.get(f"{JIRA_URL}/rest/api/3/search/jql").mock(
        return_value=httpx.Response(410)
    )
    respx.get(f"{JIRA_URL}/rest/api/3/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "issues": [
                    {
                        "key": "PROBE-7",
                        "fields": {
                            "summary": "Checkout 500s",
                            "status": {"name": "Open"},
                        },
                    }
                ]
            },
        )
    )
    async with JiraClient(jira_creds) as client:
        results = await client.search('project = "PROBE"')

    assert results[0].key == "PROBE-7"
    assert results[0].status == "Open"
    assert results[0].url == f"{JIRA_URL}/browse/PROBE-7"
    assert results[0].source == "jira"
