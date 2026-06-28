# No-Data Hallucination Analysis

## What the Output Reveals

This postmortem was generated without real telemetry data.
The system was triggered manually with no actual failure injected,
no services sending traces, and no Prometheus metrics populated.

Ghost Debugger ran all 7 agents and produced a structured report.
The PROBLEM is not the code. The problem is that the agents had nothing
real to analyze, so they fabricated plausible-sounding data.

This is the classic AI hallucination failure mode:
given empty tools, the LLM invents data that looks like real output.

---

## Diagnose Each Problem

### Problem 1: Severity is UNKNOWN

The triage agent calls tool_query_error_rate and tool_query_latency_p99.
With no Prometheus data, those tools return empty TimeSeries objects
(latest_value=0.0, is_anomalous=False). The triage agent saw normal metrics
and defaulted to UNKNOWN with triage_confirmed_services=[].

### Problem 2: Root cause confidence is 1.0 on fabricated data

The root cause agent received fabricated-but-coherent findings from
the three parallel agents. Internally consistent story led to high
confidence. Confidence 1.0 is a hallucination artifact.

**Core issue:** Empty tool results -> LLM fills in plausible numbers ->
internally consistent fake narrative -> high confidence.

### Problem 3: "unknown_service" in cascade path

Jaeger had no traces. The LLM invented "unknown_service" as a placeholder
rather than saying "no traces found."

### Problem 4: All timestamps identical

No real events were found, so the LLM used the only timestamp available:
the incident detection time.

### Problem 5: "Service 1, Service 2, Service 3"

The metric reasoner fabricated service names and values instead of
writing metric_had_error=False.

---

## The Fix

### Principle

The agents must check whether tool results contain actual data before
asking the LLM to reason over them. Empty tool results -> LLM invents
data -> hallucinated postmortem with 100% confidence.

### Fix locations

1. `agents/shared/node_utils.py` - Add is_empty_tool_result() and
   summarize_empty_signals()
2. `agents/triage/agent.py` - Preflight data check before ReAct loop;
   return early with empty confirmed_services if no data exists
3. `agents/postmortem_writer/agent.py` - No-data detection at top;
   generate "cannot analyze" report instead of running LLM

### How it stops hallucination

| Before | After |
|--------|-------|
| Triage sees empty tools, LLM invents services | Preflight check returns early, empty services |
| Pipeline routes to parallel analysis | Routes to postmortem writer immediately |
| Parallel agents each fabricate findings | Parallel agents never run |
| Root cause gets fake narrative, 1.0 confidence | No-data template says "Cannot analyze" |

---

## Resume Implication

This is a talking point, not a failure:

INTERVIEWER: "Did Ghost Debugger ever produce wrong output?"

ANSWER: "Yes - and finding it taught me something important. When I
triggered an analysis without any services running, the agents received
empty tool results. Instead of saying 'no data available,' the LLM
fabricated a coherent but completely invented narrative with 100%
confidence.

I fixed it in three places: a preflight data check in the triage agent
that returns early if tools are empty, a conditional edge that routes
to the postmortem writer immediately with a 'no data' report, and an
explicit no-data report template that tells the operator what to check.

This is the most important reliability lesson from the project: LLMs
do not say 'I don't know' - they say 'here is a plausible answer.'
The system has to detect the no-data condition before the LLM sees it."
