from bughound_agent import BugHoundAgent
from llm_client import MockClient


class GarbageIssuesClient:
    """Returns syntactically valid JSON with content that fails validation
    (missing msg, no valid severity) — should be treated as untrustworthy."""

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        if "Return ONLY valid JSON" in system_prompt:
            return '[{"type": "Bug"}]'
        return "# GarbageIssuesClient: no rewrite available.\n"


def test_workflow_runs_in_offline_mode_and_returns_shape():
    agent = BugHoundAgent(client=None)  # heuristic-only
    code = "def f():\n    print('hi')\n    return True\n"
    result = agent.run(code)

    assert isinstance(result, dict)
    assert "issues" in result
    assert "fixed_code" in result
    assert "risk" in result
    assert "logs" in result

    assert isinstance(result["issues"], list)
    assert isinstance(result["fixed_code"], str)
    assert isinstance(result["risk"], dict)
    assert isinstance(result["logs"], list)
    assert len(result["logs"]) > 0


def test_offline_mode_detects_print_issue():
    agent = BugHoundAgent(client=None)
    code = "def f():\n    print('hi')\n    return True\n"
    result = agent.run(code)

    assert any(issue.get("type") == "Code Quality" for issue in result["issues"])


def test_offline_mode_proposes_logging_fix_for_print():
    agent = BugHoundAgent(client=None)
    code = "def f():\n    print('hi')\n    return True\n"
    result = agent.run(code)

    fixed = result["fixed_code"]
    assert "logging" in fixed
    assert "logging.info(" in fixed


def test_mock_client_forces_llm_fallback_to_heuristics_for_analysis():
    # MockClient returns non-JSON for analyzer prompts, so agent should fall back.
    agent = BugHoundAgent(client=MockClient())
    code = "def f():\n    print('hi')\n    return True\n"
    result = agent.run(code)

    assert any(issue.get("type") == "Code Quality" for issue in result["issues"])
    # Ensure we logged the fallback path
    assert any("Falling back to heuristics" in entry.get("message", "") for entry in result["logs"])


def test_garbage_llm_issues_force_fallback_to_heuristics():
    # Client returns valid JSON, but every item fails content validation
    # (no msg, no recognized severity), so the agent should distrust it
    # and fall back to heuristics rather than accept a vacuous issue.
    agent = BugHoundAgent(client=GarbageIssuesClient())
    code = "def f():\n    print('hi')\n    return True\n"
    result = agent.run(code)

    assert any(issue.get("type") == "Code Quality" for issue in result["issues"])
    assert any("Falling back to heuristics" in entry.get("message", "") for entry in result["logs"])


def test_heuristic_ignores_bare_except_inside_docstring():
    # A bare "except:" that only appears inside a docstring's illustrative example
    # (not real code) must not be reported as a Reliability issue, and the code
    # must be left unchanged — regex-only heuristics used to "fix" the docstring text.
    code = (
        'def divide(a, b):\n'
        '    """\n'
        '    Example of bad style (do not copy):\n'
        '        try:\n'
        '            return a / b\n'
        '        except:\n'
        '            return None\n'
        '    """\n'
        '    if b == 0:\n'
        '        return None\n'
        '    return a / b\n'
    )
    agent = BugHoundAgent(client=None)  # heuristic-only, no API calls
    result = agent.run(code)

    assert not any(issue.get("type") == "Reliability" for issue in result["issues"])
    assert result["fixed_code"] == code
