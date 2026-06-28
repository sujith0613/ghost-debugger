import json
import logging
from typing import List, Optional, Dict
from datetime import datetime, timezone

from langchain_core.messages import HumanMessage, AIMessage, ToolMessage, SystemMessage
from langchain_core.tools import BaseTool

logger = logging.getLogger(__name__)


def now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def format_state_for_prompt(state: dict, fields: List[str]) -> str:
    lines = []
    for field in fields:
        value = state.get(field)
        if value is None:
            continue
        if isinstance(value, list) and not value:
            continue
        if isinstance(value, str) and not value:
            continue
        if isinstance(value, list):
            lines.append(f"{field}:")
            for item in value:
                lines.append(f"  - {item}")
        else:
            lines.append(f"{field}: {value}")
    return "\n".join(lines) if lines else "No prior findings available."


def run_react_loop(
    llm_with_tools,
    tools: List[BaseTool],
    messages: List,
    agent_name: str,
    max_iterations: int = 8,
) -> tuple:
    tool_map = {t.name: t for t in tools}
    tool_calls_made = []
    current_messages = list(messages)

    for iteration in range(max_iterations):
        try:
            response = llm_with_tools.invoke(current_messages)
        except Exception as e:
            logger.error(f"[{agent_name}] LLM call failed on iteration {iteration}: {e}")
            raise

        current_messages.append(response)

        tool_calls = getattr(response, "tool_calls", []) or []

        if not tool_calls:
            final_text = response.content or ""
            logger.debug(f"[{agent_name}] ReAct loop complete after {iteration + 1} iterations")
            return final_text, tool_calls_made

        for tool_call in tool_calls:
            tool_name = tool_call.get("name", "")
            tool_args = tool_call.get("args", {})
            tool_call_id = tool_call.get("id", f"call_{iteration}")

            logger.info(f"[{agent_name}] calling tool: {tool_name}({json.dumps(tool_args)[:100]})")
            tool_calls_made.append(f"{tool_name}({json.dumps(tool_args)[:100]})")

            if tool_name in tool_map:
                try:
                    tool_result = tool_map[tool_name].invoke(tool_args)
                except Exception as e:
                    tool_result = json.dumps({
                        "error": True, "tool": tool_name, "message": str(e),
                    })
                    logger.warning(f"[{agent_name}] tool {tool_name} raised: {e}")
            else:
                tool_result = json.dumps({"error": True, "message": f"Unknown tool: {tool_name}"})

            current_messages.append(ToolMessage(
                content=str(tool_result),
                tool_call_id=tool_call_id,
            ))

    last_response = current_messages[-1] if current_messages else None
    final_text = ""
    if hasattr(last_response, "content"):
        final_text = last_response.content or ""

    logger.warning(f"[{agent_name}] max iterations ({max_iterations}) reached")
    return final_text, tool_calls_made


def build_base_messages(system_prompt: str, human_prompt: str) -> List:
    return [
        SystemMessage(content=system_prompt),
        HumanMessage(content=human_prompt),
    ]


def safe_append(existing, new_items) -> list:
    result = list(existing) if existing else []
    if isinstance(new_items, list):
        result.extend(new_items)
    elif new_items:
        result.append(new_items)
    return result

def is_empty_tool_result(result_str: str) -> bool:
    if not result_str or not result_str.strip():
        return True
    try:
        data = json.loads(result_str)
    except (json.JSONDecodeError, TypeError):
        return False
    if data.get("error"):
        return True
    if "total_traces" in data and data["total_traces"] == 0:
        return True
    if "total_entries" in data and data["total_entries"] == 0:
        return True
    if "data_point_count" in data and data["data_point_count"] == 0:
        return True
    if "latest_value" in data and data["latest_value"] == 0.0:
        if "is_anomalous" in data and not data["is_anomalous"]:
            return True
    return False


def summarize_empty_signals(
    trace_empty: bool,
    log_empty: bool,
    metric_empty: bool,
) -> str:
    available = []
    missing = []
    if not trace_empty:
        available.append("traces")
    else:
        missing.append("traces")
    if not log_empty:
        available.append("logs")
    else:
        missing.append("logs")
    if not metric_empty:
        available.append("metrics")
    else:
        missing.append("metrics")
    if not available:
        return (
            "NO OBSERVABILITY DATA. Jaeger, Prometheus, and log store "
            "all returned empty results. Do NOT invent data."
        )
    parts = []
    if available:
        parts.append(f"Available: {', '.join(available)}")
    if missing:
        parts.append(f"MISSING: {', '.join(missing)} â€” do not fabricate values")
    return " | ".join(parts)
