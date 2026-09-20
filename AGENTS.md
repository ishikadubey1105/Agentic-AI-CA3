# How the agents work

This is the file to read before the viva. It answers three questions:
**which agents exist**, **which one handles a given request**, and
**how the decision is made**.

---

## 1. The idea

A single "AI chatbot" that also does maths is not an agent system. AttendX is
built the way agent systems are actually built:

- **one orchestrator** (Supervisor) that owns *decisions* and owns no data,
- **five worker agents** that own *capabilities* and make no decisions,
- a **tool registry** that is the only bridge between them.

The supervisor never calculates a percentage. The AnalyticsAgent never decides
whether the question needed a percentage. That separation is why the system can
answer "which agent handled this" for every request.

---

## 2. The agents

| # | Agent | Owns | Reasoning type | Tools |
|---|-------|------|----------------|-------|
| 1 | **DataAgent** | reading the ERP tables into one clean frame, scoped to the signed-in teacher; CSV/Excel import | deterministic (schema + validation rules) | — |
| 2 | **AnalyticsAgent** | every number: aggregates, weekly-trend forecast, feasibility maths, anomaly detection | deterministic (pandas / numpy) | 9 |
| 3 | **InsightAgent** | the written narrative on the dashboard and in the report | LLM if key, rule engine otherwise | — |
| 4 | **CommsAgent** | parent warning letters | LLM wording if key, template otherwise | 1 |
| 5 | **ReportAgent** | the PDF and the workbook | deterministic (matplotlib / openpyxl) | 1 |
| 6 | **Supervisor** | routing, ordering, final wording | **LLM function calling** if key, keyword planner otherwise | — |

In code this table is not a comment — it is the `AGENT_SPECS` dictionary in
`backend.py`, and `TOOL_OWNER` is derived from it:

```python
TOOL_OWNER = {t: a for a, spec in AGENT_SPECS.items() for t in spec["tools"]}
```

So when a tool runs, the trace can name the **agent** that owns it, not just the
function. That is what the "Agent system" screen renders.

---

## 3. What happens when you ask a question

```
  teacher types a question
            │
            ▼
   ┌────────────────────┐   1. scope the data to this teacher's subjects
   │    DataAgent       │──────────────────────────────────────────────┐
   └────────────────────┘                                              │
            │                                                          │
            ▼                                                          │
   ┌────────────────────┐   2. decide WHO should handle this           │
   │    Supervisor      │   (LLM tool call, or keyword planner)        │
   └────────────────────┘                                              │
       │        │      │                                               │
       ▼        ▼      ▼                                               │
  Analytics  Comms  Report    3. the owning agent executes its tool    │
       │        │      │                                               │
       └────────┴──────┘                                               │
            │                                                          │
            ▼                                                          │
   ┌────────────────────┐   4. tool output goes back to the supervisor │
   │    Supervisor      │◄─────────────────────────────────────────────┘
   └────────────────────┘   5. it loops if more tools are needed,
            │                  then writes the answer from tool output only
            ▼
        the answer + the full trace
```

### With your Groq key (the real agentic path)

1. The supervisor sends the question **plus all eleven tool JSON schemas** to
   Groq with `tool_choice: "auto"`.
2. Groq replies with `tool_calls` — the model has chosen the tools and filled in
   the arguments itself (e.g. `classes_needed({"student":"23CS104","target":75})`).
3. The supervisor looks up `TOOL_OWNER[tool]`, logs `delegate to AnalyticsAgent`,
   and executes it.
4. Each result is appended as a `role:"tool"` message and the loop runs again —
   up to `MAX_STEPS = 4` rounds, so the model can chain tools (ask for the
   forecast, see the result, then ask for letters).
5. When the model stops calling tools, its final text is the answer. Token
   counts and latency of every round are recorded and shown in the UI.

### Without a key (the fallback)

The same eleven tools are selected by a keyword planner over the question text.
The execution path, the agents, the trace and the answer format are identical —
only the *chooser* changes. This is deliberate: a demo must not die because the
wifi did.

---

## 4. Proving it in the viva

Open **Agent system** in the sidebar:

- **Reasoning engine** card — shows whether the LLM path or the fallback is
  active. Press **Test Groq key**: it calls `GET /openai/v1/models`, picks a live
  model, sends a one-token probe and reports the round-trip in milliseconds.
- **Last question · which agents ran** — the exact chain, e.g.
  `Supervisor → DataAgent → CommsAgent → AnalyticsAgent`, with the tools used,
  the number of LLM rounds and the tokens spent.
- **Agent topology** — every agent card lights up if it took part in the last
  question; each card lists the tools it owns and how many times it has ever run.
- **Tool registry** — all eleven tools with their owner agent and parameters.
- **Supervisor system prompt** — the actual prompt, on screen.

And on the **AI assistant** screen, every answer carries the agent chain under
it, and the right-hand panel prints the step-by-step trace:

```
01 Supervisor      · route  | receive question
02 DataAgent       · result | load scoped frame      → 2610 records, 5 subjects
03 Supervisor      · route  | plan with LLM          → 11 schemas sent
04 Supervisor      · route  | model selected tools   → risk_forecast · 612 ms
05 AnalyticsAgent  · result | risk_forecast          → 4 students projected below 75%
06 Supervisor      · route  | delegate to CommsAgent → draft_warning_letters()
07 CommsAgent      · result | draft_warning_letters  → 4 letters written
08 Supervisor      · step   | final answer           → 2 rounds, 1840 in / 210 out tokens
```

---

## 5. Design decisions worth defending

**Why the LLM is not allowed to compute.** The system prompt forbids it and the
tools are the only source of numbers. An LLM asked "what is 95/174 as a
percentage" will often be close but wrong; `AnalyticsAgent` is never wrong. The
model's job is routing and phrasing, which is what it is good at.

**Why tools are owned by agents.** Function calling alone gives you a list of
functions. Attaching each function to an agent gives you accountability: the
audit table can answer "how many times has CommsAgent run this semester" with a
`GROUP BY`.

**Why a fallback planner exists.** Agent systems in production always have a
degraded mode. Here it also doubles as the offline demo path.

**Why the trace is a first-class feature.** In an ERP, "the computer decided" is
not acceptable for something that triggers a letter to a parent. Every decision,
tool call, and result is written to the `audit` table with the actor's username.

---

## 6. Where to look in the code

| What | Where in `backend.py` |
|---|---|
| Agent registry and tool ownership | `AGENT_SPECS`, `TOOL_OWNER` |
| The eleven tool functions | `t_class_summary` … `t_compare_subjects`, collected in `TOOLS` |
| The JSON schemas sent to Groq | `TOOL_SPECS` |
| The system prompt | `SYSTEM_PROMPT` |
| The function-calling loop | `Supervisor._llm` |
| The fallback planner | `Supervisor._offline` |
| Tool execution + agent attribution | `Supervisor._run` |
| Per-request memory and trace | `class Ctx` |
| Groq client, model fallback, key test | `class LLM` |
