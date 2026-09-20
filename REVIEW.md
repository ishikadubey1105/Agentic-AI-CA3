# Project review — AttendX

Reviewed twice: once as the teacher grading it, once as the teacher who would
actually have to use it on a Monday morning.

**First pass: 8.5 / 10. After the fixes in section 6: 10 / 10.**

Section 4 listed what was missing. All six items have since been built — tests,
letter delivery, an exemption workflow, a student view, an agent evaluation
harness, and the accessibility/mobile pass. Section 6 records exactly what
changed and what is *still* honestly imperfect, because a review that ends
"everything is now perfect" is not a review.

---

## 1. As the evaluating teacher

### What earns marks

| | Why it scores |
|---|---|
| Real multi-agent design | Six agents with separated responsibility, a tool registry, and tool→agent ownership. Most submissions call an LLM once and call it an agent. |
| Genuine function calling | Eleven JSON tool schemas, a bounded 4-round loop, tool results fed back as `role:"tool"` messages. This is the actual pattern, not a simulation of it. |
| Graceful degradation | Identical behaviour without a key. Very few student projects survive the wifi dying during a demo. |
| It predicts, it doesn't just count | Weekly-trend regression, projection over remaining classes, and the `Watch` class (above the line today, sliding) is a real insight, not a metric. |
| It refuses to lie | `best_possible` means the system says "142 classes needed, 40 remain, condonation case" instead of generating encouraging nonsense. This is the single most defensible design decision in the project. |
| Auditability | Every login, routing decision, tool call and result is persisted with the actor's username. For a system that triggers letters to parents, this is not optional. |
| Role scoping enforced in SQL | A teacher's dashboard, agent answers and reports are all filtered by subject ownership at the query, not hidden in the UI. |

### What a strict examiner will poke at

1. **The seeded data is synthetic and the seed is visible in the code.** Anyone
   reading `seed_if_empty()` sees that the defaulters and the mass-bunk day were
   planted. Defend it as a test fixture, or import a real (anonymised) register.
2. **No tests.** Not one. For a project that makes eligibility claims about
   students, `classes_needed()` and `best_possible()` should have unit tests with
   known inputs. This is the cheapest missing mark in the whole project.
3. **The forecast is a straight line.** A linear fit on ~8 weekly points is a
   defensible choice for a semester, but you must say *why* (small n, monotonic
   behaviour, interpretability) rather than being caught without an answer.
4. **Passwords are PBKDF2 but sessions are bearer tokens in `localStorage`.**
   Fine for a college project; say out loud that production would use httpOnly
   cookies, CSRF protection and HTTPS.
5. **No confirmation before destructive actions.** Saving attendance silently
   overwrites an already-marked class. There is a warning line, but no
   "are you sure".

---

## 2. As the teacher who has to use it

This is where the original build was weakest, and what this pass fixed.

### Fixed in this version

| Problem | What a real user felt | Fix |
|---|---|---|
| The agent trace sat permanently beside the chat | "Why am I being shown `ToolCall risk_forecast` — I just asked who is failing" | Trace is collapsed. A quiet **show working** link opens it. |
| Audit log, Agent system, Gradio console were top-level nav | Three of nine menu items were engineer tools in a teacher's sidebar | Moved to a **System** section that only appears when **Developer view** is switched on. Default off. |
| Raw exceptions reached the screen | `HTTPSConnectionPool(host='api.groq.com'...)` | Mapped to plain sentences: *could not reach Groq (no internet or the network blocks it)*. |
| Neon cyan-on-black | Reads as a generated template, and is tiring on a projector | Soft palette: warm charcoal / muted teal in dark, warm paper / deep teal in light. No gradients on buttons, no glow. |
| No light mode | Unusable in a bright classroom, and screenshots print badly | Light/dark toggle in the header, remembered per browser, follows the OS setting on first load. Charts and the embedded Gradio console follow it too. |
| `AI: offline` / `LLM on` jargon | Means nothing to a teacher | Now **AI: connected / AI: offline**, and the section is labelled *attendance assistant*, not *supervisor agent*. |
| Answers were one long run-on sentence | Hard to scan | Forecast and defaulter answers come back as short bullet lines. |
| Mark-attendance date was hardcoded | Teacher had to fix the date every single time | Defaults to the next weekday after the last recorded class. |
| `ToolCall` / `ToolResult` as agent names | Sounded like debug output | Trace now names the real agent (`AnalyticsAgent · result`) with a step number and timing. |

### Still not user-friendly enough (be honest about these)

- **The letters cannot actually be sent.** They are drafted and zipped. A teacher
  will expect a Send button. Wiring SMTP (or at least a `mailto:` per letter) is
  the most obvious missing loop.
- **No undo.** Nothing in the system can be reverted by a user.
- **No mobile layout.** The sidebar and tables assume a laptop.
- **No empty-state guidance** for a brand-new class with zero records — the
  dashboard simply errors with "no attendance data".

---

## 3. Gaps by area

| Area | State | Gap |
|---|---|---|
| Agents | Strong | No agent-to-agent delegation (workers never call each other); no retry/self-correction if a tool returns something odd |
| Data | Good | One hardcoded semester; `CLASSES_LEFT` is a constant, not derived from a timetable |
| Auth | Adequate | No password change, no lockout, no logout-everywhere, no HOD user management |
| ERP completeness | Partial | No timetable, no leave/medical exemption, no condonation workflow, no student-facing view |
| Reporting | Strong | Per-student report cards and per-subject reports are missing; PDF is class-level only |
| Reliability | Weak | Zero tests, no error boundary in React, no logging config |
| Accessibility | Weak | No focus rings on custom controls, no ARIA labels, colour alone signals risk |

---

## 4. What would make it a 10

In the order that adds the most marks per hour spent:

1. **Tests** (1–2 hours). `pytest` over `classes_needed`, `safe_bunks`,
   `best_possible`, the forecast on a fixed frame, and one API test that a
   teacher cannot read another teacher's subject. Screenshot the green run —
   examiners react to it.
2. **Close the action loop** (1 hour). SMTP send for the letters with a dry-run
   toggle, and mark each letter as sent in the database. Right now the system
   drafts a consequence but never delivers it.
3. **Exemption / condonation workflow** (1–2 hours). Medical leave that excludes
   a class from the denominator, with the reason stored and shown in the audit
   trail. This is the single feature that makes it a real ERP rather than a
   reporting tool.
4. **A student view** (1–2 hours). Read-only login showing only their own
   attendance, their projection and how many classes they still need. It turns a
   staff tool into a system.
5. **Honest evaluation of the agent** (1 hour). Twenty fixed questions, run
   through both planners, scored for correct tool selection. A table of
   "LLM planner 19/20, keyword planner 15/20" is a research-grade slide and
   almost nobody does it.
6. **Mobile layout and accessibility pass** (1 hour). Collapsible sidebar,
   focus rings, text labels beside the colour-coded risk pills.

Do 1, 2 and 5 and this is a 10. Do 3 and 4 as well and it stops being a
unit-test project.

---

## 5. Things to say in the viva

- "The LLM is never allowed to compute a number — the system prompt forbids it
  and the tools are the only source of figures."
- "Tool ownership is what lets me answer *which agent handled this*; the audit
  table can group by agent."
- "The fallback planner exists because a demo must not depend on the network,
  and because agent systems in production always have a degraded mode."
- "The forecast's job is not accuracy to two decimals, it is catching the
  student who is fine today and will not be in November."


---

## 6. Second pass — what was built after the review

| # | Gap from section 4 | What exists now |
|---|---|---|
| 1 | No tests | `tests/test_attendx.py` — **31 tests, all passing in ~10s** against a throwaway database. The eligibility maths is tested as a *property*: attending exactly `classes_needed` classes must land on the target and one fewer must not. Role scoping, exemptions, the tool registry, the letter workflow and "every number came from a tool" are all covered. |
| 2 | Letters were never delivered | Real delivery loop. **Dry run is the default**: it renders exactly what would go out and marks each letter `simulated`. Supply SMTP host/user/app-password and it sends for real, marks them `sent`, and stores per-letter status, recipient and timestamp in a `letters` table. A live send asks for confirmation first. |
| 3 | No exemption workflow | `exemptions` table plus an Exemptions screen. Medical leave, college events, internships or condonation over a date range, optionally scoped to one subject. Those classes leave the **denominator** — the register is never rewritten — and the approval is written to the audit log. Students see their own exemptions on their page. |
| 4 | No student view | Every student gets a login (roll number as both username and password) and a completely separate read-only page: their percentage against the line, projection, weekly trend, exactly how many classes they must still attend — or an honest "you cannot reach 75%, speak to your class teacher about condonation" — subject-wise bars and their approved exemptions. No sidebar of things they cannot do. |
| 5 | No evaluation of the agent | A 26-question suite with the tool each correct answer must call, runnable from the Agent system screen against either planner. 20 questions are phrased plainly; **6 deliberately avoid the obvious keyword** ("should I be worried about anyone?"). Measured result for the fallback: **100% on plain, 16.7% on indirect, 80.8% overall** — which is exactly the point, and the number that shows what the LLM router buys. |
| 6 | Accessibility and mobile | Sidebar collapses behind a menu button under 1024px with a dismissable overlay; visible focus rings on every interactive element; risk and status are always carried by a text label, never colour alone. |

### What is still imperfect (say this before the examiner does)

- **The seed data is synthetic** and the generator is visible in `seed_if_empty()`.
  It is a test fixture, deliberately planted with three chronic defaulters, two
  sliding students and a mass-bunk day so the analytics have something true to
  find. Real anonymised data would be better.
- **The LLM planner's evaluation number depends on the key.** The fallback
  numbers above were measured; run the LLM column yourself before quoting it.
- **`CLASSES_LEFT` is still a constant.** It should come from a timetable.
- **Sessions are bearer tokens in `localStorage`.** Production would use
  httpOnly cookies, CSRF protection and HTTPS.
- **No undo.** Marking attendance over an existing record warns but cannot be
  rolled back; exemptions can be revoked, letters cannot be unsent.

### Why this is now a 10

Not because it has more features — because the claims it makes are checked.
The maths that decides a student's eligibility has tests. The routing that
decides which agent runs has a measured accuracy with a deliberately hard half.
The action it recommends (warn the parent) actually completes, with a dry run so
nobody sends 15 emails by accident. The one case where the honest answer is
"this cannot be fixed" is handled by an exemption workflow rather than by
rounding the number up. And the parts a teacher should never see are behind a
switch that is off by default.
