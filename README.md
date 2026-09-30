# Automated Attendance Report Generator

A Gradio-based agentic AI mini-project: upload a class attendance sheet, and
an agent pipeline analyzes it, writes a formatted report, answers follow-up
questions, and remembers past uploads across sessions.

## Setup

1. Create an environment (per the course's Anaconda module) and activate it:
   ```
   conda create -n attendance-agent python=3.11 -y
   conda activate attendance-agent
   ```
2. Install dependencies:
   ```
   pip install -r requirements.txt
   ```
3. Copy `.env.example` to `.env` and paste in your Groq API key
   (get one free at console.groq.com):
   ```
   GROQ_API_KEY=gsk_xxxxxxxxxxxx
   ```
4. Run it:
   ```
   python app.py
   ```
   Open the local URL Gradio prints (usually http://127.0.0.1:7860).

## Using it

1. Click **"Or load sample data"** for an instant demo, or upload your own CSV
   in the format: `Name, Roll No, <date1>, <date2>, ...` with each cell
   `P`/`A` (or `Present`/`Absent`, `1`/`0`).
2. Adjust the **defaulter threshold** slider (default 75%).
3. Click **Generate Report** for a full Markdown report you can download.
4. Switch to **Ask Questions** to chat: "who's below 70%?", "how does this
   compare to my last upload?", "which day had the worst turnout?".
5. The **Session ID** box is the memory key. Copy it out, close the app,
   reopen later, paste the same ID back in -- the conversation history picks
   up right where it left off, because it's stored in SQLite, not in RAM.

## How this maps to the syllabus

| Concept taught | Where it lives here |
|---|---|
| AI agent with tools | `analyst_agent` in `agent_core.py` -- calls `compute_attendance_stats` instead of the LLM guessing numbers |
| `FunctionTool` | `attendance_tools.py` -- `compute_attendance_stats`, `save_attendance_snapshot`, `get_attendance_history` |
| SQLite-based persistent memory / session state | `SQLiteSession` in `agent_core.run_turn` (conversation memory) + the custom `attendance_history` table in `db.py` (long-term data memory), both in the same `attendance_memory.db` file |
| Multi-turn queries over memory | The **Ask Questions** chat tab -- same `session_id` across turns |
| Agent-as-tool design | `analyst_agent.as_tool(...)` wired into `writer_agent`'s tools in `agent_core.py` |
| Handoff-style delegation (Planner -> Writer) | `writer_agent` never computes numbers itself; it always calls the analyst tool first, then writes |
| Guardrails (boundaries) | `guardrails.py` -- a small classifier agent trips an `InputGuardrail` on any off-topic message |
| Environment setup / Anaconda | `README.md` setup steps above |

**Why Groq instead of raw OpenAI:** the OpenAI Agents SDK talks to any
OpenAI-compatible endpoint. `config.py` points its default client at Groq's
`/openai/v1` endpoint via `set_default_openai_client` and switches the SDK to
the Chat Completions shape with `set_default_openai_api("chat_completions")`
(Groq doesn't implement OpenAI's newer Responses API). Everything else --
`Agent`, `Runner`, tools, sessions, guardrails -- is the exact same code you'd
write against OpenAI directly.

**Why not AutoGen / CrewAI / LangGraph / MCP here:** those are separate
frameworks covered later/elsewhere in the course. Mixing them into one
"simple, effective" project adds framework sprawl without adding to what's
being demonstrated -- one coherent OpenAI Agents SDK pipeline shows the same
core ideas (tools, memory, delegation, guardrails) more clearly and is much
easier to explain in a viva.

## Project structure

```
config.py             # wires the Agents SDK to Groq, loads .env
db.py                 # SQLite table of past attendance snapshots
attendance_tools.py   # pandas parsing/stats logic + function tools
guardrails.py         # off-topic input guardrail
agent_core.py         # builds the Analyst + Writer agents, run_turn()
app.py                # Gradio UI
sample_data.csv       # demo dataset (12 students, 10 sessions)
```

## Viva talking points

- Walk through one full request: upload -> `writer_agent` gets the guardrail
  check -> calls `analyze_attendance` (the analyst-as-tool) -> analyst calls
  `compute_attendance_stats` -> analyst calls `save_attendance_snapshot` ->
  writer formats the Markdown report.
- Show the guardrail tripping: ask the chat "what's the capital of France?"
  and it should refuse.
- Show persistent memory: generate a report, close the app, reopen, paste the
  same Session ID, and ask "what was the overall percentage last time?" --
  it should recall it via `get_attendance_history` even after a restart.
