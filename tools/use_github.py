"""GitHub GraphQL API integration tool for Strands Agents.

Provides a universal interface to GitHub's v4 GraphQL API, allowing execution of
any query or mutation. Handles authentication via GITHUB_TOKEN, parameter validation,
response formatting, and user-friendly error messages.

Source: https://github.com/cagataycali/devduck
"""

import json
import logging
import os
from typing import Any

import requests
from strands import tool

logger = logging.getLogger(__name__)

GITHUB_GRAPHQL_URL = "https://api.github.com/graphql"

MUTATIVE_KEYWORDS = [
    "create", "update", "delete", "add", "remove", "merge", "close", "reopen",
    "lock", "unlock", "pin", "unpin", "transfer", "archive", "unarchive",
    "enable", "disable", "accept", "decline", "dismiss", "submit", "request",
    "cancel", "convert",
]


def get_github_token() -> str | None:
    return os.environ.get("GITHUB_TOKEN", "")


def is_mutation_query(query: str) -> bool:
    query_lower = query.lower().strip()
    if query_lower.startswith("mutation"):
        return True
    return any(keyword in query_lower for keyword in MUTATIVE_KEYWORDS)


def execute_github_graphql(
    query: str, variables: dict[str, Any] | None = None, token: str | None = None
) -> dict[str, Any]:
    if not token:
        raise ValueError(
            "GitHub token is required. Set GITHUB_TOKEN environment variable."
        )
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/vnd.github.v4+json",
        "User-Agent": "Strands-Agent-GitHub-Tool/1.0",
    }
    payload = {"query": query, "variables": variables or {}}
    response = requests.post(GITHUB_GRAPHQL_URL, headers=headers, json=payload, timeout=30)
    response.raise_for_status()
    return response.json()


def format_github_response(response: dict[str, Any]) -> str:
    parts = []
    if "errors" in response:
        parts.append("Errors:")
        for error in response["errors"]:
            parts.append(f"  - {error.get('message', 'Unknown error')}")
    if "data" in response:
        parts.append("Data:")
        parts.append(json.dumps(response["data"], indent=2))
    if "extensions" in response and "cost" in response["extensions"]:
        cost = response["extensions"]["cost"]
        parts.append(f"Rate Limit - Cost: {cost.get('requestedQueryCost', 'N/A')}")
        if "rateLimit" in cost:
            parts.append(f"  Remaining: {cost['rateLimit'].get('remaining', 'N/A')}")
    return "\n".join(parts)


@tool
def use_github(
    query_type: str,
    query: str,
    label: str,
    variables: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute GitHub GraphQL API operations.

    Provides universal access to GitHub's GraphQL API (v4) for queries and mutations.
    Handles authentication via GITHUB_TOKEN env var, validates parameters, and formats responses.

    Args:
        query_type: Type of GraphQL operation ("query" or "mutation")
        query: The GraphQL query or mutation string
        label: Human-readable description of the operation
        variables: Optional variables dict for the query

    Returns:
        Dict with status and content keys.
    """
    if variables is None:
        variables = {}

    bypass_consent = os.environ.get("BYPASS_TOOL_CONSENT", "").lower() == "true"

    logger.info(f"GitHub GraphQL [{query_type}]: {label}")

    github_token = get_github_token()
    if not github_token:
        return {
            "status": "error",
            "content": [{"text": "GitHub token not found. Set GITHUB_TOKEN env var."}],
        }

    is_mutative = query_type.lower() == "mutation" or is_mutation_query(query)
    if is_mutative and not bypass_consent:
        confirm = input(f"Mutative operation ({label}). Proceed? [y/*] ")
        if confirm.lower() != "y":
            return {"status": "error", "content": [{"text": f"Canceled: {confirm}"}]}

    try:
        response = execute_github_graphql(query, variables, github_token)
        formatted = format_github_response(response)
        status = "error" if "errors" in response else "success"
        return {"status": status, "content": [{"text": formatted}]}
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 401:
            return {"status": "error", "content": [{"text": "Auth failed. Check GITHUB_TOKEN."}]}
        elif e.response.status_code == 403:
            return {"status": "error", "content": [{"text": f"Forbidden: {e}"}]}
        return {"status": "error", "content": [{"text": f"HTTP Error: {e}"}]}
    except Exception as e:
        logger.warning(f"GitHub GraphQL exception: {type(e).__name__}")
        return {"status": "error", "content": [{"text": f"Error: {e!s}"}]}
