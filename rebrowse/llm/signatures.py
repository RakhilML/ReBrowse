"""DSPy signatures for rebrowse LLM calls."""

from __future__ import annotations

import dspy


class ParseIntent(dspy.Signature):
    """Extract structured intent from a natural language query about a website API."""

    user_query: str = dspy.InputField(desc="Natural language query like 'get trending repos from github'")
    domain: str = dspy.OutputField(desc="Website domain (e.g. 'github.com') or empty if unclear")
    action: str = dspy.OutputField(desc="What the user wants to do in 5-10 words")
    params: dict = dspy.OutputField(desc="Specific parameters mentioned (empty dict if none)")


class PickEndpoint(dspy.Signature):
    """Pick the best API endpoint for a user's intent from a list of available endpoints."""

    intent: str = dspy.InputField(desc="User's action/intent in natural language")
    endpoints: list[dict] = dspy.InputField(desc="Available API endpoints with id, method, url, description")
    endpoint_id: str = dspy.OutputField(desc="The id of the chosen endpoint")
    params: dict = dspy.OutputField(desc="Path parameter values to substitute (empty dict if none)")
    query: dict = dspy.OutputField(desc="Query parameter values (empty dict if none)")
    reason: str = dspy.OutputField(desc="One-line explanation of why this endpoint was picked")
