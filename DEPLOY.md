# Deploying AttendX

## Short version

**Vercel will not work for this app.** Use **Render** (easiest) or
**Hugging Face Spaces** (also free, Docker). Both files are already in the repo:
`render.yaml` and `Dockerfile`.

---

## Why Vercel fails

The build error (`No FastAPI entrypoint found`) is fixable in one line, but three
things after it are not:

| Vercel's model | What AttendX needs |
|---|---|
| Serverless functions, filesystem read-only except `/tmp`, wiped between invocations | SQLite writes to `data/attendx.db` and must persist between requests — logins, attendance, exemptions, letters |
| Each request may hit a cold container | The database would re-seed on every cold start; a mark saved in one request could vanish on the next |
| ~250 MB unzipped bundle limit | `gradio` + `pandas` + `matplotlib` + `numpy` blow past that |
| No long-running process | The Gradio console mounted at `/agent` expects a live server |

Vercel is built for stateless frontends and small API functions. This is a
stateful long-running app. Using it here would mean tearing out the database,
the report generation and the Gradio console — i.e. deleting most of the project.

---

## Option 1 — Render (recommended, ~5 minutes)

1. Go to [render.com](https://render.com) and sign in **with GitHub**.
2. **New → Web Service** → pick `ishikadubey1105/Agentic-AI-CA3`.
3. Render reads `render.yaml` and fills everything in. Confirm it shows:
   - Runtime **Python 3**
   - Build command `pip install -r requirements.txt`
   - Start command `python backend.py`
   - Instance type **Free**
4. Optional: under **Environment**, add `GROQ_API_KEY` with your key so the
   deployed version routes with the LLM. (Without it the fallback planner runs,
   which is fine.)
5. **Create Web Service.** First build takes 5–10 minutes — gradio and matplotlib
   are large. Watch the log until it prints `AttendX running on http://0.0.0.0:...`.
6. Your URL is `https://attendx-xxxx.onrender.com`.

**Two things to know about the free tier:**

- It **sleeps after 15 minutes** of no traffic. The next visit takes ~50 seconds
  to wake. Open the link a minute before you demo.
- The disk is **ephemeral** — a redeploy or a restart wipes `data/attendx.db` and
  the app re-seeds itself automatically. Fine for a demo, not for real records.
  Attach a Render Disk (paid) or point it at Postgres if you ever need real
  persistence.

## Option 2 — Hugging Face Spaces (Docker)

Good if you want it to live next to your other student projects.

1. [huggingface.co/new-space](https://huggingface.co/new-space) → SDK **Docker** →
   Blank → Public.
2. Push this repo to the Space remote, or connect the GitHub repo in Space
   settings. The `Dockerfile` here already listens on 7860, which is what Spaces
   expects.
3. Add `GROQ_API_KEY` under **Settings → Variables and secrets** if you want the
   LLM path.

## Option 3 — Railway / Fly.io

Both read the same `Dockerfile`. Railway: New Project → Deploy from GitHub repo →
it detects the Dockerfile. Nothing else to configure.

---

## Before you make it public

The seeded logins (`hod / admin123`, and every student's roll number as their own
password) are in the README and in the source. On a public URL anyone can sign in
and read the demo data — which is fake, so nothing is at risk, but say so if
someone asks rather than being caught out.

If you want it locked down for a graded demo, change the passwords in
`ensure_student_logins()` and the seed block in `seed_if_empty()` before
deploying, and delete `data/attendx.db` so it re-seeds with the new ones.
