# AttendX — Automated Attendance Report Generator (Agentic AI ERP)

Agentic AI and Automation · Unit Test 3 · B.Tech CSE (AI & ML), SIT Nagpur

An attendance ERP with teacher authentication and a **supervisor AI agent** that
decides which of its eleven tools to run from a question asked in plain English.

---

## 1. Run it

```bash
pip install -r requirements.txt
python backend.py
```

Open **http://127.0.0.1:8000**

| Login     | Password  | Sees                                               |
|-----------|-----------|----------------------------------------------------|
| `ishika`  | `teach123`| only the subjects allotted to her (Agentic AI, ML)  |
| `hod`     | `admin123`| every subject, plus student/subject management      |
| `23cs104` | `23cs104` | a student: their own attendance only, read-only     |

Every student gets a login automatically — username and password are both their
roll number in lower case.

On the first run the database is created and seeded with 15 students,
5 subjects and ~2600 attendance records for July–August 2026, so there is
real data to demo. The seed includes 3 chronic defaulters, 2 students who
start well and slide, and one mass-bunk day — planted so the forecasting and
anomaly agents have something to find.

The interface has a **light and a dark theme** (toggle in the top bar; it follows
your system setting on first load). Engineer-facing screens — the agent system
view, the activity log and the Gradio console — are hidden behind the
**Developer view** switch at the bottom of the sidebar, so a teacher sees seven
menu items, not ten.

Everything works **offline**. Paste a Groq API key in the top bar (`offline mode`
button) and the same agents are driven by an LLM with real function calling
instead of the rule-based planner.

---

## 2. Using your Groq key

Click **offline mode** in the top bar, paste the key, press **Test key** — it
asks Groq which models the key can use, picks a live one and reports the round
trip. From then on the supervisor routes with real function calling; the
**Agent system** screen shows which agents ran, how many LLM rounds it took and
how many tokens were spent.

`AGENTS.md` explains the whole agent flow — read that one before the viva.
`REVIEW.md` is an honest critique of the project with a score and the gaps.

## 3. Tests

```bash
pip install pytest
pytest -q          # 31 tests, ~10 seconds
```

The suite runs against a throwaway database in a temp folder, so it never
touches your data. It covers the eligibility maths (including the property that
attending exactly `classes_needed` classes lands on the target and one fewer
does not), the forecast, exemptions leaving the denominator, role scoping
(a teacher cannot read another teacher's subject, a student sees only their own
rows), tool/agent registry consistency, the letter workflow, and the fact that
every number in an agent answer came from a tool result.

## 4. Architecture

```
React SPA (frontend/index.html)          <- ERP screens, Chart.js visuals
        |  REST + Bearer token
FastAPI (backend.py)                     <- auth, ERP API, agent endpoint
        |
   Supervisor agent  --- 11 registered tools ---> Analytics / Comms / Report agents
        |
   SQLite (data/attendx.db)              <- teachers, students, subjects,
                                            attendance, sessions, audit
        |
   Gradio console mounted at /agent      <- same agents, raw console for demos
```

### The six agents

| Agent | Responsibility |
|---|---|
| **DataAgent** | pulls the ERP tables into one clean frame, imports CSV/Excel registers, normalises messy column names and dates |
| **AnalyticsAgent** | student/subject/day aggregates, linear-trend forecasting, feasibility maths, anomaly detection |
| **InsightAgent** | writes the narrative (LLM when a key is present, rule engine otherwise) |
| **CommsAgent** | drafts one parent warning letter per defaulter with their real figures |
| **ReportAgent** | builds the 4-page PDF and the 5-sheet workbook |
| **Supervisor** | reads the question, picks tools, runs them, answers only from tool output |

### The eleven tools

`class_summary · student_report · list_defaulters · subject_report ·
classes_needed · risk_forecast · detect_anomalies · draft_warning_letters ·
generate_report · attendance_on_date · compare_subjects`

Each one is registered with a JSON schema (OpenAI/Groq function-calling format)
in `TOOL_SPECS`. With a key, the model picks them; without a key, a keyword
planner picks the same tools. Either way the answer is assembled from tool
output only — the agent never invents a number.

---

## 5. What makes it more than a percentage calculator

- **Forecasting** — fits a line through each student's weekly attendance and
  projects it over the classes still left. Flags students who are *above* the
  cut-off today but sliding (`Watch`), which is the whole point of an early
  warning system.
- **Feasibility maths** — `classes_needed` vs `best_possible`. If a student
  needs 142 classes and only 40 remain, the agent says so and calls it a
  condonation case instead of giving impossible advice.
- **Anomaly detection** — mass-absence days (2σ below the norm), students who
  dropped 20+ points in the last fortnight, and subjects the whole class avoids.
- **Role-scoped data** — a teacher's queries, dashboards and reports only ever
  touch their own subjects; enforced in SQL, not in the UI.
- **Audit trail** — every login, tool call, mark and report lands in the `audit`
  table and is visible on the Activity log screen.
- **Exemptions** — approved medical leave, college events or internships remove
  those classes from the denominator instead of faking a present mark. The
  register is never rewritten; the exemption sits beside it and is auditable.
- **Letters actually get delivered** — dry run by default (renders exactly what
  would be sent, marks each letter `simulated`), real SMTP when you supply
  credentials, and every letter's status is stored so "was the parent told" is
  answerable.
- **Routing evaluation** — 26 fixed questions with the tool a correct answer
  must call, runnable against either planner from the Agent system screen.

---

## 6. Screens

**Staff:** Dashboard · Mark attendance · Ask the assistant · Students ·
Subjects · Exemptions · Letters · Reports.
**Developer view (off by default):** Agent system (topology + routing
evaluation) · Activity log · Agent console (embedded Gradio).
**Student:** a single read-only page — their percentage, projection, how many
classes they still need, subject split and approved exemptions.

## 7. Files

```
backend.py               FastAPI app, all six agents, tool layer, auth, DB
frontend/index.html      the entire React SPA (no build step)
frontend/vendor/         React, Chart.js, Tailwind, Babel served locally
AGENTS.md                how the six agents work and who handles what
REVIEW.md                self-review: score, weaknesses, what was fixed
tests/test_attendx.py    31 tests - run with `pytest -q`
standalone_gradio_app.py the earlier single-file Gradio version, kept as a fallback
sample_attendance.csv    a register in the format the importer accepts
data/attendx.db          created on first run
outputs/                 generated PDF, workbook and warning letters
```

Delete `data/attendx.db` to reset the whole system and re-seed.
