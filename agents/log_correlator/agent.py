import logging
import re
from agents.state.postmortem_state import PostmortemState
from agents.shared.llm import get_llm_with_tools
from agents.shared.node_utils import (
    run_react_loop, build_base_messages, safe_append,
)
from agents.tools.registry import LOG_CORRELATOR_TOOLS

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are the LOG ANALYSIS AGENT in an automated incident analysis system.

You specialize in log analysis. Your job is to find specific error messages
and patterns that explain the mechanism of failure.

YOUR RESPONSIBILITIES:
1. Query ERROR logs for each confirmed affected service
2. Identify the dominant error message patterns
3. Find the FIRST occurrence of each error pattern
4. Identify specific error types that reveal the failure mechanism:
   - "connection pool exhausted" -> resource saturation
   - "connection refused" -> downstream service down
   - "deadline exceeded" / "context deadline exceeded" -> timeout cascade
   - "out of memory" / "OOM" -> memory exhaustion
   - "too many open files" -> file descriptor exhaustion
   - "dial tcp" errors -> network connectivity issue
5. Cross-reference log timestamps with incident detection time

OUTPUT FORMAT:

## Log Analysis Findings
- [finding with counts and timestamps]

## Error Patterns
PATTERN_1: "[error message pattern]" - N occurrences - first seen: [time]
PATTERN_2: "[error message pattern]" - N occurrences - first seen: [time]

## First Error Time
FIRST_ERROR_LOG: [ISO 8601 timestamp or approximate]

## Failure Mechanism
Based on log patterns, the likely failure mechanism is: [explanation]"""


def log_correlator_node(state: PostmortemState) -> dict:
    logger.info(f"[log_correlator] starting — incident: {state['incident_id']}")

    confirmed_services = state.get("triage_confirmed_services") or state.get("affected_services", [])
    time_window = state.get("triage_time_window", "")

    human_prompt = f"""LOG ANALYSIS TASK:

Incident ID: {state['incident_id']}
Trigger: {state.get('trigger_description', '')}
Confirmed Affected Services: {', '.join(confirmed_services)}
Time Window: {time_window}

Query ERROR and WARN logs for each affected service.
Find the dominant error patterns and when they first appeared.
Identify the specific failure mechanism from the log messages."""

    llm = get_llm_with_tools(LOG_CORRELATOR_TOOLS)
    messages = build_base_messages(SYSTEM_PROMPT, human_prompt)

    try:
        final_text, tool_calls = run_react_loop(
            llm_with_tools=llm,
            tools=LOG_CORRELATOR_TOOLS,
            messages=messages,
            agent_name="log_correlator",
            max_iterations=6,
        )

        findings = _extract_findings(final_text)
        error_patterns = _extract_error_patterns(final_text)
        first_error_time = _extract_first_error_time(final_text)

        logger.info(f"[log_correlator] complete — patterns found: {len(error_patterns)}")

        return {
            "log_findings": findings,
            "log_error_patterns": error_patterns,
            "log_first_error_time": first_error_time,
            "log_had_error": True,
            "completed_agents": safe_append(state.get("completed_agents", []), "log_correlator"),
        }

    except Exception as e:
        logger.error(f"[log_correlator] failed: {e}")
        return {
            "log_findings": [f"Log analysis unavailable: {str(e)[:200]}"],
            "log_error_patterns": [],
            "log_first_error_time": "",
            "log_had_error": False,
            "failed_agents": safe_append(state.get("failed_agents", []), "log_correlator"),
            "errors": safe_append(state.get("errors", []), f"[log_correlator] {type(e).__name__}: {str(e)[:200]}"),
        }


def _extract_findings(text: str) -> list:
    findings = []
    section = re.search(r"## Log Analysis Findings\n(.*?)(?:\n##|\Z)", text, re.DOTALL | re.IGNORECASE)
    if section:
        for line in section.group(1).strip().split("\n"):
            line = line.strip().lstrip("- ").strip()
            if line:
                findings.append(line)
    return findings or [text[:500]]


def _extract_error_patterns(text: str) -> list:
    patterns = []
    for match in re.finditer(r"PATTERN_\d+:\s*(.+?)$", text, re.MULTILINE):
        patterns.append(match.group(1).strip())
    if not patterns:
        for match in re.finditer(r'"([^"]{10,100})"', text):
            patterns.append(match.group(1))
    return patterns[:10]


def _extract_first_error_time(text: str) -> str:
    match = re.search(r"FIRST_ERROR_LOG:\s*(.+?)$", text, re.MULTILINE)
    if match:
        return match.group(1).strip()
    ts_match = re.search(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}', text)
    return ts_match.group(0) if ts_match else ""
