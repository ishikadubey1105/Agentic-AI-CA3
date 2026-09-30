"""
Assembles the agent pipeline:

    Attendance Report Writer (guardrail on input)
        -> uses Attendance Analyst as a tool
              -> Analyst uses compute_attendance_stats / get_attendance_history /
                 save_attendance_snapshot as function tools

This mirrors the "Planner -> Writer" / agent-as-tool pattern from the
syllabus: the Writer doesn't compute anything itself, it delegates the
number-crunching to a specialist Analyst agent and focuses on turning
structured results into a readable report or answer.
"""
import config  # noqa: F401  (must run first: wires the Agents SDK to Groq)

from agents import Agent, InputGuardrail, Runner, SQLiteSession

try:
    from agents import InputGuardrailTripwireTriggered
except ImportError:  # pragma: no cover - fallback for older/newer SDK layouts
    from agents.exceptions import InputGuardrailTripwireTriggered

import db
from attendance_tools import (
    AttendanceContext,
    compute_attendance_stats,
    get_attendance_history,
    save_attendance_snapshot,
)
from config import DB_PATH, MODEL
from guardrails import attendance_topic_guardrail

db.init_db()

analyst_agent = Agent(
    name="Attendance Analyst",
    instructions=(
        "You are an attendance data analyst. Always start by calling "
        "compute_attendance_stats to crunch the currently loaded CSV -- never guess "
        "numbers. Call get_attendance_history if a comparison with earlier uploads "
        "would help. After computing fresh stats, call save_attendance_snapshot once "
        "so future sessions can compare against this one. Reply with a concise, "
        "structured summary of the numbers (not prose commentary)."
    ),
    tools=[compute_attendance_stats, get_attendance_history, save_attendance_snapshot],
    model=MODEL,
)

writer_agent = Agent(
    name="Attendance Report Writer",
    instructions=(
        "You are the assistant behind an Automated Attendance Report Generator for a "
        "college class. For any request about the uploaded data -- generating a "
        "report, checking who's below the threshold, session trends, comparisons "
        "with earlier uploads -- call the analyze_attendance tool to get fresh, "
        "accurate numbers; never invent numbers yourself.\n\n"
        "When asked to generate the report, produce a well-formatted Markdown report "
        "with these sections: '## Executive Summary', '## Overall Attendance', "
        "'## Defaulters (below threshold)' as a Markdown table with Name / Roll No / %, "
        "'## Session-wise Trend', '## Comparison with Previous Records' (only if "
        "history exists), and '## Recommendations' for the class teacher.\n\n"
        "For follow-up chat questions, answer directly and concisely using the tool's "
        "numbers instead of repeating the full report."
    ),
    tools=[
        analyst_agent.as_tool(
            tool_name="analyze_attendance",
            tool_description=(
                "Runs full attendance analysis on the currently loaded CSV: overall %, "
                "per-student %, defaulters, session trend, and comparison with saved "
                "history."
            ),
        )
    ],
    input_guardrails=[InputGuardrail(guardrail_function=attendance_topic_guardrail)],
    model=MODEL,
)


def _session(session_id: str) -> SQLiteSession:
    return SQLiteSession(session_id, DB_PATH)


async def run_turn(message: str, context: AttendanceContext, session_id: str) -> str:
    """Run one turn of the pipeline. Conversation memory persists per
    session_id across turns *and* across app restarts, since it's backed by
    the same SQLite file as the attendance history table."""
    try:
        result = await Runner.run(
            writer_agent, message, context=context, session=_session(session_id)
        )
        return result.final_output
    except InputGuardrailTripwireTriggered:
        return (
            "This assistant only handles classroom attendance analysis and "
            "reporting -- please ask about attendance %, defaulters, trends, or "
            "request the report."
        )
    except Exception as exc:  # noqa: BLE001 - surface parsing/format errors to the UI
        return f"Something went wrong: {exc}"
