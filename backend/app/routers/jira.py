"""Jira discovery endpoints backing the UI's project and issue-type pickers."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from ..clients.jira import JiraClient
from ..deps import jira_client, require_api_key, stored_jira
from ..models import JiraCredentials

router = APIRouter(prefix="/api/jira", tags=["jira"], dependencies=[Depends(require_api_key)])


@router.get("/projects")
async def list_projects(
    query: str = Query("", description="Filter by name or key"),
    client: JiraClient = Depends(jira_client),
) -> dict:
    return {"projects": await client.projects(query=query)}


@router.get("/issue-types")
async def list_issue_types(
    project_key: str | None = Query(None),
    client: JiraClient = Depends(jira_client),
    jira: JiraCredentials = Depends(stored_jira),
) -> dict:
    key = project_key or jira.project_key
    return {"project_key": key, "issue_types": await client.issue_types(key)}


@router.get("/permissions")
async def check_permissions(
    project_key: str | None = Query(None),
    client: JiraClient = Depends(jira_client),
    jira: JiraCredentials = Depends(stored_jira),
) -> dict:
    key = project_key or jira.project_key
    permissions = await client.check_permissions(key)
    return {
        "project_key": key,
        "permissions": permissions,
        "can_create": all(permissions.values()),
    }


@router.get("/myself")
async def myself(client: JiraClient = Depends(jira_client)) -> dict:
    me = await client.myself()
    return {
        "account_id": me.get("accountId"),
        "display_name": me.get("displayName"),
        "email": me.get("emailAddress"),
        "active": me.get("active"),
        "timezone": me.get("timeZone"),
    }
