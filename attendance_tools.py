"""
The Attendance Analyst agent's tools. All the actual number-crunching happens
in plain pandas here -- the agent's job is to decide *when* to call this tool
and how to talk about the results, not to do arithmetic itself.

The CSV data and the defaulter threshold are passed in through a
RunContextWrapper (AttendanceContext) rather than as LLM-visible tool
arguments, so the model can't "forget" or mis-type the data it's working on.
"""
from __future__ import annotations

import io
import json
from dataclasses import dataclass

import pandas as pd
from agents import RunContextWrapper, function_tool

import db

PRESENT_TOKENS = {"P", "PRESENT", "1", "YES", "Y"}
ABSENT_TOKENS = {"A", "ABSENT", "0", "NO", "N"}


@dataclass
class AttendanceContext:
    csv_text: str
    threshold: float = 75.0
    label: str = "Uploaded Sheet"


class AttendanceFormatError(ValueError):
    """Raised when the uploaded file doesn't look like an attendance sheet."""


def _parse_csv(csv_text: str) -> pd.DataFrame:
    try:
        df = pd.read_csv(io.StringIO(csv_text))
    except Exception as exc:
        raise AttendanceFormatError(f"Could not parse the file as CSV ({exc}).") from exc
    if df.shape[1] < 3 or df.shape[0] < 1:
        raise AttendanceFormatError(
            "Expected a Name column, an optional Roll No column, and at least two "
            "date/session columns -- this file only has "
            f"{df.shape[1]} column(s) and {df.shape[0]} row(s)."
        )
    return df


def _identify_columns(df: pd.DataFrame) -> tuple[list[str], list[str]]:
    """First 1-2 columns that aren't P/A-like values are treated as identity
    columns (name, roll no); everything else is a session/date column."""
    id_cols = []
    for col in df.columns[:2]:
        values = df[col].astype(str).str.strip().str.upper()
        looks_like_attendance = values.isin(PRESENT_TOKENS | ABSENT_TOKENS).all()
        if not looks_like_attendance:
            id_cols.append(col)
    if not id_cols:
        id_cols = [df.columns[0]]
    session_cols = [c for c in df.columns if c not in id_cols]
    if not session_cols:
        raise AttendanceFormatError("No date/session columns were found after the name column(s).")
    return id_cols, session_cols


def compute_stats(df: pd.DataFrame, threshold: float) -> dict:
    id_cols, session_cols = _identify_columns(df)
    name_col = id_cols[0]
    roll_col = id_cols[1] if len(id_cols) > 1 else None

    total_cells = 0
    unknown_cells = 0
    per_student = []
    session_present_counts = {c: 0 for c in session_cols}

    for _, row in df.iterrows():
        present, total = 0, 0
        for c in session_cols:
            raw = str(row[c]).strip().upper()
            total_cells += 1
            if raw in PRESENT_TOKENS:
                present += 1
                total += 1
                session_present_counts[c] += 1
            elif raw in ABSENT_TOKENS:
                total += 1
            else:
                unknown_cells += 1
        pct = round((present / total) * 100, 2) if total else 0.0
        per_student.append(
            {
                "name": str(row[name_col]),
                "roll_no": str(row[roll_col]) if roll_col else None,
                "present": present,
                "total_sessions": total,
                "percentage": pct,
            }
        )

    if total_cells and unknown_cells / total_cells > 0.2:
        raise AttendanceFormatError(
            "Too many attendance cells could not be understood. Use P/A "
            "(or Present/Absent, 1/0) in each date column."
        )

    total_present = sum(s["present"] for s in per_student)
    total_possible = sum(s["total_sessions"] for s in per_student)
    overall_percentage = round((total_present / total_possible) * 100, 2) if total_possible else 0.0

    defaulters = sorted(
        (s for s in per_student if s["percentage"] < threshold),
        key=lambda s: s["percentage"],
    )

    n_students = len(df)
    session_wise = [
        {
            "date": str(c),
            "present_count": session_present_counts[c],
            "percentage": round((session_present_counts[c] / n_students) * 100, 2) if n_students else 0.0,
        }
        for c in session_cols
    ]

    trend = "stable"
    if len(session_wise) >= 4:
        half = len(session_wise) // 2
        first_avg = sum(s["percentage"] for s in session_wise[:half]) / half
        second_avg = sum(s["percentage"] for s in session_wise[half:]) / (len(session_wise) - half)
        if second_avg - first_avg > 3:
            trend = "improving"
        elif first_avg - second_avg > 3:
            trend = "declining"

    return {
        "total_students": n_students,
        "total_sessions": len(session_cols),
        "overall_percentage": overall_percentage,
        "threshold": threshold,
        "per_student": per_student,
        "defaulters": defaulters,
        "session_wise": session_wise,
        "trend": trend,
        "best_day": max(session_wise, key=lambda s: s["percentage"]) if session_wise else None,
        "worst_day": min(session_wise, key=lambda s: s["percentage"]) if session_wise else None,
    }


@function_tool
async def compute_attendance_stats(ctx: RunContextWrapper[AttendanceContext]) -> str:
    """Parse the currently loaded attendance CSV and compute overall attendance %,
    per-student %, defaulters below the configured threshold, session-wise
    numbers, and the trend across sessions. Always call this before answering
    any question about the data."""
    df = _parse_csv(ctx.context.csv_text)
    stats = compute_stats(df, ctx.context.threshold)
    return json.dumps(stats)


@function_tool
async def save_attendance_snapshot(ctx: RunContextWrapper[AttendanceContext], stats_json: str) -> str:
    """Persist the just-computed attendance stats (the JSON string returned by
    compute_attendance_stats) into long-term SQLite history, so a future
    session can compare against it."""
    stats = json.loads(stats_json)
    row_id = db.save_snapshot(ctx.context.label, stats["overall_percentage"], stats_json)
    return f"Saved snapshot #{row_id} (label='{ctx.context.label}', {stats['overall_percentage']}% overall)."


@function_tool
async def get_attendance_history(ctx: RunContextWrapper[AttendanceContext], limit: int = 5) -> str:
    """Fetch the most recently saved attendance snapshots (label, date saved,
    overall %) so the current numbers can be compared against earlier
    uploads."""
    return json.dumps(db.get_recent_snapshots(limit))
