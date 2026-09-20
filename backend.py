"""
AttendX - Attendance ERP with an AI agent
Agentic AI and Automation, Unit Test 3

Backend: FastAPI + SQLite. Serves the React frontend, exposes the ERP API and
mounts the Gradio agent console at /agent.

Run:
    pip install fastapi uvicorn gradio pandas numpy matplotlib requests openpyxl python-multipart
    python backend.py

then open  http://127.0.0.1:8000

Default logins (created on first run):
    ishika / teach123     - teacher, owns Agentic AI + Machine Learning
    hod    / admin123     - HOD, sees every subject and can manage students
"""

import os
import re
import io
import json
import time
import random
import sqlite3
import hashlib
import secrets
import zipfile
import datetime as dt
from contextlib import closing

import numpy as np
import pandas as pd

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages

import requests
import uvicorn
import gradio as gr
from fastapi import FastAPI, Depends, HTTPException, Header, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE = os.path.dirname(os.path.abspath(__file__))
# the data and output folders can be redirected, which is what the test
# suite does so it never touches the real database
DATA_DIR = os.environ.get("ATTENDX_DATA", os.path.join(BASE, "data"))
OUT_DIR = os.environ.get("ATTENDX_OUT", os.path.join(BASE, "outputs"))
MAIL_DIR = os.path.join(OUT_DIR, "letters")
FRONTEND = os.path.join(BASE, "frontend", "index.html")
DB_PATH = os.path.join(DATA_DIR, "attendx.db")
for d in (DATA_DIR, OUT_DIR, MAIL_DIR):
    os.makedirs(d, exist_ok=True)

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.3-70b-versatile"

THRESHOLD_DEFAULT = 75
CLASSES_LEFT = 40                 # classes still to be conducted this semester
SESSION_HOURS = 12


# ======================================================================
#  Database
# ======================================================================
SCHEMA = """
CREATE TABLE IF NOT EXISTS teachers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE, name TEXT, role TEXT,
    salt TEXT, pwd_hash TEXT, created TEXT);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY, teacher_id INTEGER, expires TEXT);

CREATE TABLE IF NOT EXISTS students (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    roll TEXT UNIQUE, name TEXT, email TEXT, active INTEGER DEFAULT 1);

CREATE TABLE IF NOT EXISTS subjects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT UNIQUE, name TEXT, teacher_id INTEGER);

CREATE TABLE IF NOT EXISTS attendance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT, subject_id INTEGER, student_id INTEGER,
    present INTEGER, marked_by INTEGER, marked_at TEXT,
    UNIQUE(date, subject_id, student_id));

CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, actor TEXT, agent TEXT, action TEXT, params TEXT, outcome TEXT);

-- an approved absence does not count against the student: the record stays
-- in the register but is dropped from the denominator
CREATE TABLE IF NOT EXISTS exemptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER, subject_id INTEGER, from_date TEXT, to_date TEXT,
    reason TEXT, kind TEXT, approved_by INTEGER, created TEXT);

-- one row per drafted letter, so "was the parent actually told" is answerable
CREATE TABLE IF NOT EXISTS letters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER, roll TEXT, name TEXT, percent REAL, must_attend INTEGER,
    body TEXT, file TEXT, drafted_at TEXT, drafted_by TEXT,
    status TEXT DEFAULT 'draft', sent_at TEXT, sent_to TEXT, note TEXT);
"""


def migrate(con):
    """add columns that older databases will not have"""
    cols = {r["name"] for r in con.execute("PRAGMA table_info(teachers)")}
    if "student_id" not in cols:
        con.execute("ALTER TABLE teachers ADD COLUMN student_id INTEGER")
        con.commit()


def db():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


def hash_pwd(password, salt=None):
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120_000)
    return salt, h.hex()


def audit(actor, agent, action, params="", outcome=""):
    with closing(db()) as con:
        con.execute("INSERT INTO audit (ts, actor, agent, action, params, outcome)"
                    " VALUES (?,?,?,?,?,?)",
                    (dt.datetime.now().isoformat(timespec="seconds"), actor, agent,
                     action, str(params)[:400], str(outcome)[:400]))
        con.commit()


# ----------------------------------------------------------------------
#  First run: create the tables and fill them with a believable semester
# ----------------------------------------------------------------------
def seed_if_empty():
    with closing(db()) as con:
        con.executescript(SCHEMA)
        con.commit()
        migrate(con)
        if con.execute("SELECT COUNT(*) c FROM teachers").fetchone()["c"]:
            ensure_student_logins(con)
            return

        print("[setup] empty database, seeding demo data ...")
        for uname, name, role, pwd in [
                ("ishika", "Prof. Ishika Dubey", "teacher", "teach123"),
                ("hod", "Dr. R. Deshpande (HOD)", "hod", "admin123")]:
            salt, ph = hash_pwd(pwd)
            con.execute("INSERT INTO teachers (username,name,role,salt,pwd_hash,created)"
                        " VALUES (?,?,?,?,?,?)",
                        (uname, name, role, salt, ph,
                         dt.datetime.now().isoformat(timespec="seconds")))

        names = ["Aarav Sharma", "Ishika Dubey", "Rohan Patil", "Sneha Iyer",
                 "Kabir Mehta", "Ananya Rao", "Vivek Kulkarni", "Priya Nair",
                 "Arjun Deshmukh", "Tanvi Joshi", "Yash Agarwal", "Meera Pillai",
                 "Nikhil Verma", "Riya Bansal", "Omkar Jadhav"]
        for i, n in enumerate(names):
            roll = f"23CS{101 + i}"
            con.execute("INSERT INTO students (roll,name,email) VALUES (?,?,?)",
                        (roll, n, roll.lower() + "@sitnagpur.siu.edu.in"))

        subjects = [("CS301", "Agentic AI", 1), ("CS302", "Machine Learning", 1),
                    ("CS303", "DBMS", 2), ("CS304", "Computer Networks", 2),
                    ("HS301", "Soft Skills", 2)]
        for code, sname, tid in subjects:
            con.execute("INSERT INTO subjects (code,name,teacher_id) VALUES (?,?,?)",
                        (code, sname, tid))
        con.commit()

        # --- generate two months of attendance -------------------------
        random.seed(7)
        students = con.execute("SELECT id, name FROM students").fetchall()
        subs = con.execute("SELECT id, name FROM subjects").fetchall()

        habit = {s["id"]: random.uniform(0.80, 0.99) for s in students}
        for sid in random.sample([s["id"] for s in students], 3):
            habit[sid] = random.uniform(0.52, 0.70)          # chronic defaulters
        sliding = random.sample([s["id"] for s in students if habit[s["id"]] > 0.85], 2)

        day, end = dt.date(2026, 7, 1), dt.date(2026, 8, 30)
        span = (end - day).days
        rows = []
        while day <= end:
            if day.weekday() < 5:
                progress = (day - dt.date(2026, 7, 1)).days / span
                mass_bunk = random.random() < 0.04
                for sub in subs:
                    if random.random() < 0.15:
                        continue
                    for s in students:
                        p = habit[s["id"]]
                        if sub["name"] == "Soft Skills":
                            p *= 0.88
                        if s["id"] in sliding:
                            p *= (1 - 0.30 * progress)
                        if mass_bunk:
                            p *= 0.35
                        rows.append((day.isoformat(), sub["id"], s["id"],
                                     1 if random.random() < p else 0, 1,
                                     day.isoformat() + "T10:00:00"))
            day += dt.timedelta(days=1)

        con.executemany("INSERT OR IGNORE INTO attendance "
                        "(date,subject_id,student_id,present,marked_by,marked_at)"
                        " VALUES (?,?,?,?,?,?)", rows)
        con.commit()

        # a couple of approved medical leaves so the exemption feature has
        # something real to show on day one
        low = con.execute("SELECT student_id, AVG(present) p FROM attendance "
                          "GROUP BY student_id ORDER BY p LIMIT 2").fetchall()
        for r in low:
            con.execute("INSERT INTO exemptions (student_id,subject_id,from_date,"
                        "to_date,reason,kind,approved_by,created) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (r["student_id"], None, "2026-08-03", "2026-08-07",
                         "Hospitalised - certificate submitted", "medical", 2,
                         dt.datetime.now().isoformat(timespec="seconds")))
        con.commit()
        ensure_student_logins(con)
        print(f"[setup] seeded {len(rows)} attendance records")


def ensure_student_logins(con):
    """every student gets a read-only login: username = roll, password = roll"""
    have = {r["username"] for r in con.execute("SELECT username FROM teachers")}
    made = 0
    for st in con.execute("SELECT id, roll, name FROM students WHERE active=1"):
        uname = st["roll"].lower()
        if uname in have:
            continue
        salt, ph = hash_pwd(st["roll"].lower())
        con.execute("INSERT INTO teachers (username,name,role,salt,pwd_hash,"
                    "created,student_id) VALUES (?,?,?,?,?,?,?)",
                    (uname, st["name"], "student", salt, ph,
                     dt.datetime.now().isoformat(timespec="seconds"), st["id"]))
        made += 1
    if made:
        con.commit()
        print(f"[setup] created {made} student logins (username = roll, "
              f"password = roll in lower case)")


# ======================================================================
#  Auth
# ======================================================================
def login_user(username, password):
    with closing(db()) as con:
        t = con.execute("SELECT * FROM teachers WHERE username=?",
                        (username.strip().lower(),)).fetchone()
        if not t:
            return None
        _, ph = hash_pwd(password, t["salt"])
        if not secrets.compare_digest(ph, t["pwd_hash"]):
            return None
        token = secrets.token_hex(24)
        exp = dt.datetime.now() + dt.timedelta(hours=SESSION_HOURS)
        con.execute("INSERT INTO sessions (token,teacher_id,expires) VALUES (?,?,?)",
                    (token, t["id"], exp.isoformat()))
        con.commit()
        audit(t["username"], "Auth", "login", "", "session opened")
        return {"token": token, "name": t["name"], "role": t["role"],
                "username": t["username"], "id": t["id"],
                "student_id": t["student_id"] if "student_id" in t.keys() else None}


def current_user(authorization: str = Header(default="")):
    token = authorization.replace("Bearer", "").strip()
    if not token:
        raise HTTPException(401, "not signed in")
    with closing(db()) as con:
        row = con.execute(
            "SELECT s.token, s.expires, t.* FROM sessions s "
            "JOIN teachers t ON t.id = s.teacher_id WHERE s.token=?",
            (token,)).fetchone()
    if not row or dt.datetime.fromisoformat(row["expires"]) < dt.datetime.now():
        raise HTTPException(401, "session expired, please sign in again")
    return {"id": row["id"], "username": row["username"], "name": row["name"],
            "role": row["role"],
            "student_id": row["student_id"] if "student_id" in row.keys() else None}


# ======================================================================
#  Agent 1 - DataAgent : pulls the ERP tables into one clean frame
# ======================================================================
class DataAgent:
    name = "DataAgent"

    # a class covered by an approved exemption is removed from the
    # denominator entirely - the record stays, it just stops counting
    EXEMPT_CLAUSE = (
        " AND NOT EXISTS (SELECT 1 FROM exemptions e "
        "                 WHERE e.student_id = a.student_id "
        "                   AND (e.subject_id IS NULL OR e.subject_id = a.subject_id) "
        "                   AND a.date BETWEEN e.from_date AND e.to_date)")

    def frame(self, subject_id=None, teacher=None, include_exempt=False):
        q = ("SELECT a.date AS Date, st.roll AS RollNo, st.name AS Name, "
             "sub.name AS Subject, sub.id AS SubjectId, a.present AS Present "
             "FROM attendance a "
             "JOIN students st ON st.id = a.student_id "
             "JOIN subjects sub ON sub.id = a.subject_id WHERE 1=1")
        args = []
        if not include_exempt:
            q += self.EXEMPT_CLAUSE
        if subject_id:
            q += " AND sub.id = ?"
            args.append(subject_id)
        # a plain teacher only ever sees the subjects allotted to them
        if teacher and teacher["role"] == "teacher":
            q += " AND sub.teacher_id = ?"
            args.append(teacher["id"])
        # a student only ever sees their own rows
        if teacher and teacher["role"] == "student":
            q += " AND a.student_id = ?"
            args.append(teacher.get("student_id") or -1)
        with closing(db()) as con:
            df = pd.read_sql_query(q, con, params=args)
        if len(df):
            df["Date"] = pd.to_datetime(df["Date"])
        return df

    def import_csv(self, path, actor):
        """Bulk import - the old CSV route, kept so existing sheets still work."""
        df = pd.read_csv(path) if path.lower().endswith(".csv") else pd.read_excel(path)
        alias = {"date": "Date", "rollno": "RollNo", "roll": "RollNo",
                 "prn": "RollNo", "name": "Name", "studentname": "Name",
                 "subject": "Subject", "course": "Subject",
                 "status": "Status", "attendance": "Status"}
        ren = {c: alias[re.sub(r"[^a-z]", "", str(c).lower())]
               for c in df.columns if re.sub(r"[^a-z]", "", str(c).lower()) in alias}
        df = df.rename(columns=ren)
        need = ["Date", "RollNo", "Subject", "Status"]
        missing = [c for c in need if c not in df.columns]
        if missing:
            raise ValueError("missing column(s): " + ", ".join(missing))

        parsed = pd.to_datetime(df["Date"], errors="coerce")
        if parsed.isna().mean() > 0.3:
            parsed = pd.to_datetime(df["Date"], errors="coerce", dayfirst=True)
        df["Date"] = parsed
        df["Status"] = (df["Status"].astype(str).str.strip().str.upper()
                        .replace({"P": "PRESENT", "A": "ABSENT", "1": "PRESENT",
                                  "0": "ABSENT", "YES": "PRESENT", "NO": "ABSENT"}))
        df = df.dropna(subset=["Date"])
        df = df[df["Status"].isin(["PRESENT", "ABSENT"])]

        with closing(db()) as con:
            smap = {r["roll"]: r["id"] for r in con.execute("SELECT id,roll FROM students")}
            submap = {r["name"].lower(): r["id"] for r in
                      con.execute("SELECT id,name FROM subjects")}
            rows, skipped = [], 0
            for _, r in df.iterrows():
                sid = smap.get(str(r["RollNo"]).strip())
                bid = submap.get(str(r["Subject"]).strip().lower())
                if not sid or not bid:
                    skipped += 1
                    continue
                rows.append((r["Date"].date().isoformat(), bid, sid,
                             1 if r["Status"] == "PRESENT" else 0, 1,
                             dt.datetime.now().isoformat(timespec="seconds")))
            con.executemany("INSERT OR REPLACE INTO attendance "
                            "(date,subject_id,student_id,present,marked_by,marked_at)"
                            " VALUES (?,?,?,?,?,?)", rows)
            con.commit()
        audit(actor, self.name, "import_csv", os.path.basename(path),
              f"{len(rows)} rows imported, {skipped} skipped")
        return {"imported": len(rows), "skipped": skipped}


# ======================================================================
#  Agent 2 - AnalyticsAgent : every number the ERP shows
# ======================================================================
class AnalyticsAgent:
    name = "AnalyticsAgent"

    def student_table(self, df, threshold):
        t = (df.groupby(["RollNo", "Name"])
               .agg(Classes=("Present", "size"), Attended=("Present", "sum"))
               .reset_index())
        t["Percent"] = (t["Attended"] / t["Classes"] * 100).round(2)
        t["Remark"] = t["Percent"].apply(
            lambda p: "Defaulter" if p < threshold
            else ("Borderline" if p < threshold + 8 else "Safe"))
        return t.sort_values("Percent").reset_index(drop=True)

    def subject_table(self, df):
        t = (df.groupby("Subject")
               .agg(Classes=("Present", "size"), Attended=("Present", "sum"))
               .reset_index())
        t["Percent"] = (t["Attended"] / t["Classes"] * 100).round(2)
        return t.sort_values("Percent").reset_index(drop=True)

    def daily(self, df):
        d = (df.groupby(df["Date"].dt.date)["Present"].mean() * 100).round(2)
        d = d.reset_index()
        d.columns = ["Date", "Percent"]
        d["Date"] = d["Date"].astype(str)
        return d

    def heatmap(self, df):
        p = (df.pivot_table(index="Name", columns="Subject",
                            values="Present", aggfunc="mean") * 100).round(1)
        return p

    @staticmethod
    def classes_needed(attended, total, target):
        if total and attended / total * 100 >= target:
            return 0
        need = (target * total - 100 * attended) / (100 - target)
        return int(np.ceil(max(need, 0)))

    @staticmethod
    def safe_bunks(attended, total, target):
        if total and attended / total * 100 < target:
            return 0
        return int(np.floor(max((100 * attended - target * total) / target, 0)))

    @staticmethod
    def best_possible(attended, total):
        return round((attended + CLASSES_LEFT) / (total + CLASSES_LEFT) * 100, 2)

    def forecast(self, df, threshold):
        """
        Fit a line through each student's weekly attendance and push it forward
        over the classes still left. Catches the ones who are fine today but
        sliding - that is the early warning the whole ERP exists for.
        """
        d = df.copy()
        d["Week"] = d["Date"].dt.isocalendar().week.astype(int)
        rows = []
        for (roll, name), g in d.groupby(["RollNo", "Name"]):
            weekly = g.groupby("Week")["Present"].mean() * 100
            att, tot = int(g["Present"].sum()), int(len(g))
            slope = (float(np.polyfit(np.arange(len(weekly)), weekly.values, 1)[0])
                     if len(weekly) >= 3 else 0.0)
            future = float(np.clip(weekly.iloc[-1] + slope * 2, 0, 100))
            proj = (att + future / 100 * CLASSES_LEFT) / (tot + CLASSES_LEFT) * 100
            rows.append({
                "RollNo": roll, "Name": name,
                "Current": round(g["Present"].mean() * 100, 2),
                "Trend": round(slope, 2), "Projected": round(proj, 2),
                "NeedToAttend": self.classes_needed(att, tot, threshold),
                "CanMiss": self.safe_bunks(att, tot, threshold),
                "BestPossible": self.best_possible(att, tot),
            })
        t = pd.DataFrame(rows)
        t["Recoverable"] = np.where(t["BestPossible"] < threshold, "not possible",
                           np.where(t["NeedToAttend"] > 0, "only with 100% from now",
                                    "comfortable"))
        t["Risk"] = np.where(t["Projected"] < threshold, "High",
                    np.where((t["Current"] >= threshold) & (t["Trend"] < -1.5), "Watch",
                    np.where(t["Projected"] < threshold + 6, "Medium", "Low")))
        order = {"High": 0, "Watch": 1, "Medium": 2, "Low": 3}
        return (t.sort_values(["Risk", "Projected"],
                              key=lambda c: c.map(order).fillna(c))
                 .reset_index(drop=True))

    def anomalies(self, df, threshold):
        out = []
        d = self.daily(df)
        mu, sd = d["Percent"].mean(), d["Percent"].std()
        for _, r in d[d["Percent"] < mu - 2 * sd].iterrows():
            out.append({"Type": "Mass absence", "Where": str(r["Date"]),
                        "Detail": f"only {r['Percent']}% present against a "
                                  f"{mu:.1f}% norm"})
        cut = df["Date"].max() - pd.Timedelta(days=14)
        for (roll, name), g in df.groupby(["RollNo", "Name"]):
            rec, old = g[g["Date"] > cut], g[g["Date"] <= cut]
            if len(rec) >= 5 and len(old) >= 5:
                rp, op = rec["Present"].mean() * 100, old["Present"].mean() * 100
                if op - rp >= 20:
                    out.append({"Type": "Sudden drop", "Where": f"{name} ({roll})",
                                "Detail": f"{op:.0f}% earlier, {rp:.0f}% in the "
                                          f"last two weeks"})
        s = self.subject_table(df)
        if len(s) > 2 and s.iloc[0]["Percent"] < s["Percent"].mean() - 8:
            out.append({"Type": "Weak subject", "Where": s.iloc[0]["Subject"],
                        "Detail": f"{s.iloc[0]['Percent']}% against a "
                                  f"{s['Percent'].mean():.1f}% average"})
        return pd.DataFrame(out) if out else pd.DataFrame(
            columns=["Type", "Where", "Detail"])

    def stats(self, df, threshold):
        st, sb = self.student_table(df, threshold), self.subject_table(df)
        fc = self.forecast(df, threshold)
        return {
            "records": int(len(df)), "students": int(len(st)),
            "subjects": int(len(sb)), "days": int(df["Date"].dt.date.nunique()),
            "overall": round(float(df["Present"].mean() * 100), 2),
            "threshold": threshold,
            "defaulters": int((st["Percent"] < threshold).sum()),
            "sliding": int((fc["Risk"] == "Watch").sum()),
            "high_risk": int((fc["Risk"] == "High").sum()),
            "best_subject": sb.iloc[-1]["Subject"],
            "best_pct": float(sb.iloc[-1]["Percent"]),
            "worst_subject": sb.iloc[0]["Subject"],
            "worst_pct": float(sb.iloc[0]["Percent"]),
            "lowest": f"{st.iloc[0]['Name']} ({st.iloc[0]['RollNo']})",
            "lowest_pct": float(st.iloc[0]["Percent"]),
            "from": str(df["Date"].min().date()), "to": str(df["Date"].max().date()),
            "classes_left": CLASSES_LEFT,
        }


# ======================================================================
#  LLM wrapper - Groq, optional. Everything degrades gracefully.
# ======================================================================
class LLM:
    """
    Thin Groq client. Every call records how long it took and how many tokens
    it burned, because the agent trace shows that to the user.
    """

    # tried in order - if the first model has been retired by Groq the next
    # one is used instead of the whole agent falling back to keywords
    MODELS = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant",
              "openai/gpt-oss-20b"]

    def __init__(self):
        self.key = os.environ.get("GROQ_API_KEY", "")
        self.model = GROQ_MODEL
        self.last = {}                 # ms + token counts of the last call

    @property
    def enabled(self):
        return bool(self.key and self.key.strip())

    def _post(self, payload):
        t0 = time.time()
        r = requests.post(GROQ_URL,
                          headers={"Authorization": f"Bearer {self.key.strip()}",
                                   "Content-Type": "application/json"},
                          json=payload, timeout=60)
        if r.status_code == 401:
            raise RuntimeError("Groq rejected the API key (401)")
        if r.status_code == 429:
            raise RuntimeError("Groq rate limit hit (429), try again in a moment")
        if r.status_code >= 400:
            raise RuntimeError(f"Groq error {r.status_code}: {r.text[:160]}")
        out = r.json()
        use = out.get("usage") or {}
        self.last = {"ms": int((time.time() - t0) * 1000),
                     "model": payload.get("model"),
                     "in_tokens": use.get("prompt_tokens"),
                     "out_tokens": use.get("completion_tokens")}
        return out

    def _with_fallback(self, payload):
        """if the configured model is gone, walk down the list once"""
        tried = [self.model] + [m for m in self.MODELS if m != self.model]
        last_err = None
        for m in tried:
            payload["model"] = m
            try:
                out = self._post(payload)
                self.model = m
                return out
            except RuntimeError as e:
                last_err = e
                if "model" not in str(e).lower():
                    raise            # a key or rate problem, no point retrying
        raise last_err

    def check(self, key=None):
        """used by the 'test key' button in the UI"""
        if key:
            self.key = key
        if not self.enabled:
            return {"ok": False, "error": "no API key given"}
        try:
            r = requests.get("https://api.groq.com/openai/v1/models",
                             headers={"Authorization": f"Bearer {self.key.strip()}"},
                             timeout=20)
            if r.status_code != 200:
                return {"ok": False, "error": f"Groq replied {r.status_code}: "
                                              f"{r.text[:140]}"}
            ids = [m["id"] for m in r.json().get("data", [])]
            pick = next((m for m in self.MODELS if m in ids),
                        ids[0] if ids else None)
            if not pick:
                return {"ok": False, "error": "the key works but no model is available"}
            self.model = pick
            out = self._post({"model": pick, "max_tokens": 12, "messages":
                              [{"role": "user", "content": "reply with the word ok"}]})
            return {"ok": True, "model": pick, "ms": self.last.get("ms"),
                    "reply": out["choices"][0]["message"]["content"].strip()[:40],
                    "available": len(ids)}
        except requests.exceptions.RequestException:
            return {"ok": False, "error": "could not reach api.groq.com - no "
                                          "internet, or the network is blocking it"}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    def plain(self, prompt):
        try:
            out = self._with_fallback({"temperature": 0.3, "max_tokens": 700,
                                       "messages": [{"role": "user",
                                                     "content": prompt}]})
            return out["choices"][0]["message"]["content"].strip()
        except Exception as e:
            audit("system", "LLM", "groq_error", str(e)[:120], "offline fallback used")
            return None

    def with_tools(self, messages, tools):
        return self._with_fallback({"temperature": 0.2, "max_tokens": 900,
                                    "messages": messages, "tools": tools,
                                    "tool_choice": "auto"})


LLM_ = LLM()
DATA = DataAgent()
AN = AnalyticsAgent()


# ======================================================================
#  Agent 3 - InsightAgent : the written narrative
# ======================================================================
class InsightAgent:
    name = "InsightAgent"

    def summary(self, df, threshold, actor):
        s = AN.stats(df, threshold)
        fc = AN.forecast(df, threshold)
        high = fc[fc["Risk"] == "High"]["Name"].tolist()[:5]
        watch = fc[fc["Risk"] == "Watch"]["Name"].tolist()[:5]

        if LLM_.enabled:
            facts = json.dumps({**s, "high_risk": high, "sliding": watch,
                                "anomalies": AN.anomalies(df, threshold)
                                .to_dict("records")[:5]})
            out = LLM_.plain(
                "You are an academic coordinator. Using ONLY this JSON, write five "
                "short bullet points on the class attendance and end with one "
                "recommended action. Invent no numbers.\n" + facts)
            if out:
                audit(actor, self.name, "summarise", "groq", "LLM narrative")
                return out

        bits = [
            f"- {s['students']} students tracked over {s['days']} class days "
            f"({s['records']} records) from {s['from']} to {s['to']}.",
            f"- Class attendance is {s['overall']}% against the required "
            f"{threshold}%.",
        ]
        bits.append(f"- {s['defaulters']} students are already short, the lowest "
                    f"being {s['lowest']} at {s['lowest_pct']}%."
                    if s["defaulters"] else
                    "- Every student is currently above the requirement.")
        if high:
            bits.append(f"- Projected to stay short even after {CLASSES_LEFT} more "
                        f"classes: {', '.join(high)}.")
        if watch:
            bits.append(f"- Above the line today but falling week on week: "
                        f"{', '.join(watch)}. Warn them now, not in December.")
        bits.append(f"- {s['worst_subject']} is weakest at {s['worst_pct']}% against "
                    f"{s['best_subject']} at {s['best_pct']}%; review its slot in "
                    f"the timetable.")
        audit(actor, self.name, "summarise", "offline", "rule based narrative")
        return "\n".join(bits)


# ======================================================================
#  Agent 4 - CommsAgent : parent warning letters
# ======================================================================
class CommsAgent:
    name = "CommsAgent"

    def draft(self, df, threshold, actor, limit=12):
        st = AN.student_table(df, threshold)
        fc = AN.forecast(df, threshold).set_index("RollNo")
        targets = st[st["Percent"] < threshold].head(limit)

        for f in os.listdir(MAIL_DIR):
            os.remove(os.path.join(MAIL_DIR, f))

        made = []
        for _, r in targets.iterrows():
            row = fc.loc[r["RollNo"]]
            body = self._body(r, int(row["NeedToAttend"]),
                              float(row["BestPossible"]), threshold)
            fname = f"{r['RollNo']}_warning.txt"
            with open(os.path.join(MAIL_DIR, fname), "w", encoding="utf-8") as fh:
                fh.write(body)
            made.append({"roll": r["RollNo"], "name": r["Name"],
                         "percent": float(r["Percent"]),
                         "must_attend": int(row["NeedToAttend"]),
                         "file": fname, "body": body})

        if made:
            with zipfile.ZipFile(os.path.join(OUT_DIR, "warning_letters.zip"), "w") as z:
                for m in made:
                    z.write(os.path.join(MAIL_DIR, m["file"]), m["file"])

        # persist them, so "has this parent been told" is answerable later
        with closing(db()) as con:
            smap = {r["roll"]: (r["id"], r["email"]) for r in
                    con.execute("SELECT id, roll, email FROM students")}
            con.execute("DELETE FROM letters WHERE status = 'draft'")
            for m in made:
                sid, email = smap.get(m["roll"], (None, ""))
                m["email"] = email
                cur = con.execute(
                    "INSERT INTO letters (student_id,roll,name,percent,must_attend,"
                    "body,file,drafted_at,drafted_by,status,sent_to) "
                    "VALUES (?,?,?,?,?,?,?,?,?,'draft',?)",
                    (sid, m["roll"], m["name"], m["percent"], m["must_attend"],
                     m["body"], m["file"],
                     dt.datetime.now().isoformat(timespec="seconds"), actor, email))
                m["id"] = cur.lastrowid
                m["status"] = "draft"
            con.commit()

        audit(actor, self.name, "draft_letters", f"below {threshold}%",
              f"{len(made)} letters")
        return made

    # ---- delivery ----------------------------------------------------
    def send(self, ids, actor, dry_run=True, smtp=None):
        """
        Dry run by default: it renders exactly what would be sent and marks the
        letter as 'simulated'. With real SMTP settings it actually posts the
        mail and marks it 'sent'. Either way the outcome lands in the audit log.
        """
        smtp = smtp or {}
        with closing(db()) as con:
            q = "SELECT * FROM letters"
            args = []
            if ids:
                q += " WHERE id IN (%s)" % ",".join("?" * len(ids))
                args = list(ids)
            rows = [dict(r) for r in con.execute(q, args)]

        if not rows:
            return {"sent": 0, "results": [], "dry_run": dry_run}

        server = None
        if not dry_run:
            if not (smtp.get("host") and smtp.get("user") and smtp.get("password")):
                raise ValueError("SMTP host, user and password are required to "
                                 "actually send mail - leave dry run on to preview")
            import smtplib
            server = smtplib.SMTP(smtp["host"], int(smtp.get("port") or 587), timeout=25)
            server.starttls()
            server.login(smtp["user"], smtp["password"])

        results, ok = [], 0
        for r in rows:
            to = r["sent_to"] or ""
            try:
                if dry_run:
                    status, note = "simulated", "dry run - nothing left the machine"
                else:
                    from email.mime.text import MIMEText
                    subject = r["body"].split("\n")[0].replace("Subject:", "").strip()
                    msg = MIMEText("\n".join(r["body"].split("\n")[1:]).strip())
                    msg["Subject"] = subject or f"Attendance shortage - {r['name']}"
                    msg["From"] = smtp["user"]
                    msg["To"] = to
                    server.sendmail(smtp["user"], [to], msg.as_string())
                    status, note = "sent", f"delivered to {to}"
                ok += 1
            except Exception as e:
                status, note = "failed", str(e)[:180]

            with closing(db()) as con:
                con.execute("UPDATE letters SET status=?, sent_at=?, note=? WHERE id=?",
                            (status, dt.datetime.now().isoformat(timespec="seconds"),
                             note, r["id"]))
                con.commit()
            results.append({"id": r["id"], "roll": r["roll"], "name": r["name"],
                            "to": to, "status": status, "note": note})

        if server:
            server.quit()
        audit(actor, self.name, "send_letters",
              f"dry_run={dry_run}, {len(rows)} letters", f"{ok} ok")
        return {"sent": ok, "results": results, "dry_run": dry_run}

    def _body(self, r, need, best, threshold):
        if need > CLASSES_LEFT:
            action = (f"Only {CLASSES_LEFT} classes remain this semester, so even "
                      f"with full attendance the student can reach at most {best}%. "
                      f"Please meet the class teacher regarding extra classes or "
                      f"condonation.")
        else:
            action = (f"The student must attend the next {need} of the "
                      f"{CLASSES_LEFT} remaining classes without any absence to "
                      f"become eligible.")

        if LLM_.enabled:
            out = LLM_.plain(
                "Write a short, polite, formal email from a class teacher to a "
                "student's parent about an attendance shortage. Maximum 130 words, "
                "no placeholders, use only these facts: "
                f"{r['Name']} ({r['RollNo']}) has {r['Percent']}% attendance against "
                f"a {threshold}% requirement, attending {r['Attended']} of "
                f"{r['Classes']} classes. {action}")
            if out:
                return out

        return (f"Subject: Attendance shortage - {r['Name']} ({r['RollNo']})\n\n"
                f"Dear Parent / Guardian,\n\n"
                f"This is to inform you that {r['Name']} (roll number {r['RollNo']}) "
                f"currently has {r['Percent']}% attendance, which is below the "
                f"{threshold}% required by the institute. Out of {r['Classes']} "
                f"classes conducted, {r['Attended']} were attended.\n\n"
                f"{action} Kindly ensure regular attendance from the coming week.\n\n"
                f"Regards,\nClass Teacher\nDepartment of Computer Science\n"
                f"Symbiosis Institute of Technology, Nagpur\n")


# ======================================================================
#  Agent 5 - ReportAgent : the PDF and Excel deliverables
# ======================================================================
class ReportAgent:
    name = "ReportAgent"
    BLUE, RED, GREEN, AMBER, GREY = "#2563eb", "#dc2626", "#059669", "#d97706", "#6b7280"

    def build(self, df, threshold, narrative, actor):
        s = AN.stats(df, threshold)
        st = AN.student_table(df, threshold)
        sb = AN.subject_table(df)
        fc = AN.forecast(df, threshold)
        an = AN.anomalies(df, threshold)

        xlsx = os.path.join(OUT_DIR, "attendance_report.xlsx")
        try:
            with pd.ExcelWriter(xlsx) as w:
                pd.DataFrame(s.items(), columns=["Metric", "Value"]).to_excel(
                    w, sheet_name="Summary", index=False)
                st.to_excel(w, sheet_name="Student wise", index=False)
                sb.to_excel(w, sheet_name="Subject wise", index=False)
                fc.to_excel(w, sheet_name="Risk forecast", index=False)
                an.to_excel(w, sheet_name="Anomalies", index=False)
        except Exception as e:
            xlsx = os.path.join(OUT_DIR, "attendance_report.csv")
            st.to_csv(xlsx, index=False)
            audit(actor, self.name, "excel_failed", str(e)[:80], "csv written instead")

        charts = self._charts(df, st, sb, fc, threshold)
        pdf = os.path.join(OUT_DIR, "attendance_report.pdf")
        with PdfPages(pdf) as book:
            self._cover(book, s, narrative, fc, threshold)
            self._grid(book, charts, "Charts")
            self._tables(book, st, an)
        audit(actor, self.name, "build_report", f"cut-off {threshold}%",
              "pdf + workbook written")
        return {"pdf": os.path.basename(pdf), "sheet": os.path.basename(xlsx)}

    def _charts(self, df, st, sb, fc, threshold):
        plt.rcParams.update({"figure.facecolor": "white", "axes.spines.top": False,
                             "axes.spines.right": False, "axes.grid": True,
                             "grid.color": "#e5e7eb", "axes.axisbelow": True,
                             "font.size": 9})
        paths = []

        fig, ax = plt.subplots(figsize=(7, 3.2))
        ax.barh(sb["Subject"], sb["Percent"],
                color=[self.RED if p < threshold else self.BLUE for p in sb["Percent"]])
        ax.axvline(threshold, color=self.GREY, ls="--", lw=1)
        ax.set_xlim(0, 105)
        ax.set_title("Subject wise attendance", loc="left", weight="bold")
        paths.append(self._save(fig, "c_subject.png"))

        fig, ax = plt.subplots(figsize=(7, 3.2))
        ax.bar(range(len(st)), st["Percent"],
               color=[self.RED if p < threshold else
                      (self.AMBER if p < threshold + 8 else self.GREEN)
                      for p in st["Percent"]])
        ax.axhline(threshold, color=self.GREY, ls="--", lw=1)
        ax.set_xticks(range(len(st)))
        ax.set_xticklabels(st["RollNo"], rotation=75, fontsize=6.5)
        ax.set_ylim(0, 100)
        ax.set_title("Every student against the cut-off", loc="left", weight="bold")
        paths.append(self._save(fig, "c_students.png"))

        d = AN.daily(df)
        fig, ax = plt.subplots(figsize=(7, 3.0))
        ax.plot(pd.to_datetime(d["Date"]), d["Percent"], color=self.GREY, lw=0.8,
                marker="o", ms=2.4, label="daily")
        ax.plot(pd.to_datetime(d["Date"]), d["Percent"].rolling(5, min_periods=1).mean(),
                color=self.BLUE, lw=2, label="5 day average")
        ax.axhline(threshold, color=self.RED, ls="--", lw=1, label=f"cut-off {threshold}%")
        ax.set_ylim(0, 100)
        ax.legend(frameon=False, fontsize=8)
        ax.set_title("Day wise trend", loc="left", weight="bold")
        plt.xticks(rotation=30, ha="right", fontsize=7)
        paths.append(self._save(fig, "c_trend.png"))

        fig, ax = plt.subplots(figsize=(6.2, 4.0))
        cmap = {"High": self.RED, "Watch": self.AMBER,
                "Medium": self.AMBER, "Low": self.GREEN}
        ax.scatter(fc["Current"], fc["Projected"], s=42,
                   c=[cmap[r] for r in fc["Risk"]], edgecolor="white", zorder=3)
        ax.axhline(threshold, color=self.RED, ls="--", lw=1)
        ax.axvline(threshold, color=self.RED, ls="--", lw=1)
        for _, r in fc[fc["Risk"].isin(["High", "Watch"])].iterrows():
            ax.annotate(r["RollNo"], (r["Current"], r["Projected"]), fontsize=6.5,
                        xytext=(3, 3), textcoords="offset points")
        ax.set_xlabel("attendance today %")
        ax.set_ylabel(f"projected after {CLASSES_LEFT} classes %")
        ax.set_title("Where each student is heading", loc="left", weight="bold")
        paths.append(self._save(fig, "c_forecast.png"))
        return paths

    @staticmethod
    def _save(fig, name):
        p = os.path.join(OUT_DIR, name)
        fig.tight_layout()
        fig.savefig(p, dpi=130)
        plt.close(fig)
        return p

    def _cover(self, book, s, narrative, fc, threshold):
        fig = plt.figure(figsize=(8.27, 11.69))
        fig.text(0.07, 0.945, "Attendance Report", fontsize=23, weight="bold")
        fig.text(0.07, 0.921, f"{s['from']} to {s['to']}   |   AttendX ERP",
                 fontsize=9.5, color="#666")
        fig.patches.append(plt.Rectangle((0.07, 0.912), 0.86, 0.0035,
                                         transform=fig.transFigure, color=self.BLUE))
        for i, (k, v) in enumerate([("Overall", f"{s['overall']}%"),
                                    ("Cut-off", f"{threshold}%"),
                                    ("Students", s["students"]),
                                    ("Defaulters", s["defaulters"])]):
            x = 0.07 + i * 0.218
            fig.patches.append(plt.Rectangle((x, 0.815), 0.20, 0.072,
                                             transform=fig.transFigure,
                                             facecolor="#f3f4f6", edgecolor="#e5e7eb"))
            fig.text(x + 0.012, 0.862, k, fontsize=8, color="#666")
            fig.text(x + 0.012, 0.830, str(v), fontsize=17, weight="bold")

        fig.text(0.07, 0.775,
                 f"Records analysed  : {s['records']}\n"
                 f"Subjects          : {s['subjects']}      Class days : {s['days']}\n"
                 f"Best subject      : {s['best_subject']} ({s['best_pct']}%)\n"
                 f"Weakest subject   : {s['worst_subject']} ({s['worst_pct']}%)\n"
                 f"Lowest student    : {s['lowest']} at {s['lowest_pct']}%",
                 fontsize=10, family="monospace", va="top")

        fig.text(0.07, 0.675, "Observations", fontsize=13, weight="bold")
        fig.text(0.07, 0.652, self._wrap(narrative, 90), fontsize=9.5, va="top")

        fig.text(0.07, 0.40, "Students needing action", fontsize=13, weight="bold")
        rows = [f"{'Roll':<11}{'Name':<22}{'Now':>7}{'Proj':>8}{'Attend':>8}  Risk",
                "-" * 66]
        for _, r in fc[fc["Risk"].isin(["High", "Watch"])].head(14).iterrows():
            rows.append(f"{r['RollNo']:<11}{str(r['Name'])[:20]:<22}"
                        f"{r['Current']:>6.1f}%{r['Projected']:>7.1f}%"
                        f"{r['NeedToAttend']:>7}  {r['Risk']}")
        if len(rows) == 2:
            rows.append("No student is at risk.")
        fig.text(0.07, 0.377, "\n".join(rows), fontsize=8.2, family="monospace",
                 va="top")
        fig.text(0.07, 0.04, f"Generated by AttendX on {dt.datetime.now():%d %b %Y, %H:%M}",
                 fontsize=8, color="#888")
        book.savefig(fig)
        plt.close(fig)

    def _grid(self, book, charts, title):
        for chunk in [charts[:3], charts[3:]]:
            if not chunk:
                continue
            fig = plt.figure(figsize=(8.27, 11.69))
            fig.text(0.07, 0.95, title, fontsize=15, weight="bold")
            for i, c in enumerate(chunk):
                ax = fig.add_axes([0.07, 0.64 - i * 0.30, 0.86, 0.27])
                ax.imshow(plt.imread(c))
                ax.axis("off")
            book.savefig(fig)
            plt.close(fig)
            title = "Charts (continued)"

    def _tables(self, book, st, an):
        fig = plt.figure(figsize=(8.27, 11.69))
        fig.text(0.07, 0.95, "Anomalies detected", fontsize=15, weight="bold")
        txt = ("\n".join(f"- {r['Type']}: {r['Where']} - {r['Detail']}"
                         for _, r in an.iterrows())
               if len(an) else "Nothing unusual found.")
        fig.text(0.07, 0.92, self._wrap(txt, 90), fontsize=9, va="top")

        fig.text(0.07, 0.72, "Full student list", fontsize=15, weight="bold")
        rows = [f"{'Roll':<11}{'Name':<22}{'Classes':>8}{'Present':>9}{'%':>8}  Remark",
                "-" * 70]
        for _, r in st.iterrows():
            rows.append(f"{r['RollNo']:<11}{str(r['Name'])[:20]:<22}{r['Classes']:>8}"
                        f"{r['Attended']:>9}{r['Percent']:>7.1f}%  {r['Remark']}")
        fig.text(0.07, 0.695, "\n".join(rows), fontsize=8, family="monospace", va="top")
        book.savefig(fig)
        plt.close(fig)

    @staticmethod
    def _wrap(text, width):
        out = []
        for line in str(text).split("\n"):
            while len(line) > width:
                cut = line.rfind(" ", 0, width)
                cut = cut if cut > 0 else width
                out.append(line[:cut])
                line = "   " + line[cut:].strip()
            out.append(line)
        return "\n".join(out)


INSIGHT = InsightAgent()
COMMS = CommsAgent()
REPORT = ReportAgent()


def friendly_error(e):
    """a teacher should never see a stack trace or a proxy hostname"""
    t = str(e)
    if isinstance(e, requests.exceptions.RequestException) or "ConnectionPool" in t:
        return "could not reach Groq (no internet or the network blocks it)"
    if "401" in t:
        return "the Groq API key was rejected"
    if "429" in t:
        return "Groq rate limit reached"
    return t[:120]


# ======================================================================
#  Agent registry
#  Every tool belongs to exactly one worker agent. The supervisor never
#  computes anything itself - it only decides which agent to activate, so
#  "which agent handled this question" is always answerable from the trace.
# ======================================================================
AGENT_SPECS = {
    "Supervisor": {
        "role": "Router. Reads the question, picks the agents and the order "
                "they run in, then writes the final answer from their output.",
        "kind": "orchestrator",
        "tools": [],
        "reasoning": "LLM function calling when a Groq key is present, "
                     "keyword planner otherwise",
    },
    "DataAgent": {
        "role": "Reads the ERP tables into one clean frame, scoped to the "
                "subjects the signed-in teacher owns. Imports CSV/Excel "
                "registers and repairs messy columns and dates.",
        "kind": "worker",
        "tools": [],
        "reasoning": "deterministic - schema mapping and validation rules",
    },
    "AnalyticsAgent": {
        "role": "Owns every number: aggregates, the weekly-trend forecast, "
                "feasibility maths and anomaly detection.",
        "kind": "worker",
        "tools": ["class_summary", "student_report", "list_defaulters",
                  "subject_report", "classes_needed", "risk_forecast",
                  "detect_anomalies", "attendance_on_date", "compare_subjects"],
        "reasoning": "deterministic - pandas and numpy, no model involved",
    },
    "InsightAgent": {
        "role": "Turns the analytics into a written narrative for the "
                "dashboard and the report.",
        "kind": "worker",
        "tools": [],
        "reasoning": "LLM when a key is present, rule-based sentences otherwise",
    },
    "CommsAgent": {
        "role": "Drafts one parent warning letter per defaulter using that "
                "student's real figures and recovery maths.",
        "kind": "worker",
        "tools": ["draft_warning_letters"],
        "reasoning": "LLM wording when a key is present, formal template otherwise",
    },
    "ReportAgent": {
        "role": "Builds the four page PDF and the five sheet workbook.",
        "kind": "worker",
        "tools": ["generate_report"],
        "reasoning": "deterministic - matplotlib and openpyxl",
    },
}

# tool -> the agent that owns it, so the trace can name the agent, not just
# the function
TOOL_OWNER = {t: a for a, spec in AGENT_SPECS.items() for t in spec["tools"]}


# ======================================================================
#  The tool layer the supervisor is allowed to call
# ======================================================================
class Ctx:
    """
    One request's working memory: who is asking, what cut-off applies, the data
    they are allowed to see, and the trace of everything the agents did.
    """
    def __init__(self, user, threshold=THRESHOLD_DEFAULT):
        self.user = user
        self.threshold = threshold
        self.df = DATA.frame(teacher=user)
        self.trace = []
        self.agents_used = []       # order in which agents were activated
        self.used_llm = False       # did the LLM path actually succeed this time
        self.step = 0
        self.t0 = time.time()

    def log(self, agent, action, params="", outcome="", kind="step", ms=None,
            meta=None):
        """
        kind:  route  - the supervisor deciding who should handle this
               call   - a tool being invoked on a worker agent
               result - what that agent returned
               think  - the model's own reasoning between tool calls
               step   - anything else
        """
        self.step += 1
        if agent in AGENT_SPECS and agent not in self.agents_used:
            self.agents_used.append(agent)
        self.trace.append({
            "step": self.step,
            "ts": dt.datetime.now().strftime("%H:%M:%S"),
            "agent": agent, "kind": kind, "action": action,
            "params": str(params)[:220], "outcome": str(outcome)[:400],
            "ms": ms, "meta": meta or {},
            "elapsed": int((time.time() - self.t0) * 1000),
        })
        audit(self.user["username"], agent, action, params, outcome)


def _find(ctx, query):
    st = AN.student_table(ctx.df, ctx.threshold)
    q = str(query).strip().lower()
    hit = st[st["RollNo"].str.lower() == q]
    if not len(hit):
        hit = st[st["Name"].str.lower().str.contains(q, na=False)]
    if not len(hit):
        hit = st[st["RollNo"].str.lower().str.contains(q, na=False)]
    return hit


def t_class_summary(ctx):
    s = AN.stats(ctx.df, ctx.threshold)
    return (f"From {s['from']} to {s['to']} the class attended {s['overall']}% of "
            f"{s['records']} records. {s['defaulters']} of {s['students']} students "
            f"are below {s['threshold']}%, {s['sliding']} more are above the line but "
            f"falling. Best subject {s['best_subject']} ({s['best_pct']}%), weakest "
            f"{s['worst_subject']} ({s['worst_pct']}%).")


def t_student_report(ctx, student):
    hit = _find(ctx, student)
    if not len(hit):
        return f"No student matched '{student}'."
    r = hit.iloc[0]
    fc = AN.forecast(ctx.df, ctx.threshold)
    f = fc[fc["RollNo"] == r["RollNo"]].iloc[0]
    sub = (ctx.df[ctx.df["RollNo"] == r["RollNo"]]
           .groupby("Subject")["Present"].mean() * 100).round(1)
    line = (f"{r['Name']} ({r['RollNo']}): {r['Percent']}%, attended {r['Attended']} "
            f"of {r['Classes']} classes, status {r['Remark']}. Subject wise: "
            + ", ".join(f"{k} {v}%" for k, v in sub.items())
            + f". Weekly trend {f['Trend']:+.2f}, projected {f['Projected']}% by "
              f"semester end, risk {f['Risk']}.")
    if r["Percent"] < ctx.threshold:
        line += (f" Needs {f['NeedToAttend']} more classes; recovery is "
                 f"{f['Recoverable']}.")
    else:
        line += f" Can still miss {f['CanMiss']} classes."
    return line


def t_list_defaulters(ctx, subject=None):
    df = ctx.df
    if subject:
        df = df[df["Subject"].str.lower().str.contains(subject.lower(), na=False)]
        if not len(df):
            return f"No subject matched '{subject}'."
    st = AN.student_table(df, ctx.threshold)
    d = st[st["Percent"] < ctx.threshold]
    where = f" in {subject}" if subject else ""
    if not len(d):
        return f"No student is below {ctx.threshold}%{where}."
    return (f"{len(d)} defaulters{where}, below {ctx.threshold}%:\n" +
            "\n".join(f"- {r['Name']} ({r['RollNo']}) {r['Percent']}%"
                      for _, r in d.iterrows()))


def t_subject_report(ctx, subject=None):
    sb = AN.subject_table(ctx.df)
    if subject:
        hit = sb[sb["Subject"].str.lower().str.contains(subject.lower())]
        if not len(hit):
            return "No such subject. Available: " + ", ".join(sb["Subject"])
        r = hit.iloc[0]
        return (f"{r['Subject']}: {r['Percent']}% over {r['Classes']} records "
                f"({r['Attended']} present).")
    return "Subject wise: " + "; ".join(f"{r['Subject']} {r['Percent']}%"
                                        for _, r in sb.iterrows())


def t_classes_needed(ctx, student, target=None):
    target = float(target or ctx.threshold)
    hit = _find(ctx, student)
    if not len(hit):
        return f"No student matched '{student}'."
    r = hit.iloc[0]
    n = AN.classes_needed(r["Attended"], r["Classes"], target)
    if n == 0:
        return (f"{r['Name']} is at {r['Percent']}%, above {target}%. Can miss "
                f"{AN.safe_bunks(r['Attended'], r['Classes'], target)} more classes.")
    best = AN.best_possible(r["Attended"], r["Classes"])
    if n > CLASSES_LEFT:
        return (f"{r['Name']} is at {r['Percent']}% and needs {n} classes for "
                f"{target}%, but only {CLASSES_LEFT} remain. The best reachable "
                f"figure is {best}%, so this is a condonation case.")
    return (f"{r['Name']} ({r['Percent']}%) must attend the next {n} of the "
            f"{CLASSES_LEFT} remaining classes without a break to reach {target}%.")


def t_risk_forecast(ctx):
    fc = AN.forecast(ctx.df, ctx.threshold)
    high, watch = fc[fc["Risk"] == "High"], fc[fc["Risk"] == "Watch"]
    lines = [f"{len(high)} students are projected to stay below {ctx.threshold}% "
             f"even after {CLASSES_LEFT} more classes."]
    for _, r in high.head(8).iterrows():
        lines.append(f"- {r['Name']} ({r['RollNo']}): {r['Current']}% now, "
                     f"{r['Projected']}% projected, recovery {r['Recoverable']}")
    for _, r in watch.iterrows():
        lines.append(f"- {r['Name']} ({r['RollNo']}): above the line today but "
                     f"falling {r['Trend']:+.1f} points a week")
    return "\n".join(lines)


def t_detect_anomalies(ctx):
    a = AN.anomalies(ctx.df, ctx.threshold)
    if not len(a):
        return "No anomalies found."
    return f"{len(a)} anomalies: " + "; ".join(
        f"{r['Type']} - {r['Where']} ({r['Detail']})" for _, r in a.iterrows())


def t_draft_letters(ctx):
    made = COMMS.draft(ctx.df, ctx.threshold, ctx.user["username"])
    if not made:
        return "Nobody is below the cut-off, no letters needed."
    return (f"Drafted {len(made)} letters (Letters screen): " +
            ", ".join(f"{m['name']} ({m['roll']})" for m in made))


def t_generate_report(ctx):
    narrative = INSIGHT.summary(ctx.df, ctx.threshold, ctx.user["username"])
    out = REPORT.build(ctx.df, ctx.threshold, narrative, ctx.user["username"])
    return f"Report built: {out['pdf']} and {out['sheet']} (Reports screen)."


def t_attendance_on(ctx, date):
    d = ctx.df[ctx.df["Date"].dt.date.astype(str) == str(date)[:10]]
    if not len(d):
        return f"No classes were recorded on {date}."
    pct = round(d["Present"].mean() * 100, 2)
    absent = d[d["Present"] == 0]["Name"].unique().tolist()
    return (f"On {str(date)[:10]} attendance was {pct}% across {len(d)} entries. "
            + (f"Absent: {', '.join(absent[:12])}." if absent else "Nobody was absent."))


def t_compare_subjects(ctx):
    sb = AN.subject_table(ctx.df)
    return (f"{sb.iloc[-1]['Subject']} leads at {sb.iloc[-1]['Percent']}% and "
            f"{sb.iloc[0]['Subject']} trails at {sb.iloc[0]['Percent']}%, a gap of "
            f"{sb.iloc[-1]['Percent'] - sb.iloc[0]['Percent']:.1f} points.")


TOOLS = {
    "class_summary": t_class_summary,
    "student_report": t_student_report,
    "list_defaulters": t_list_defaulters,
    "subject_report": t_subject_report,
    "classes_needed": t_classes_needed,
    "risk_forecast": t_risk_forecast,
    "detect_anomalies": t_detect_anomalies,
    "draft_warning_letters": t_draft_letters,
    "generate_report": t_generate_report,
    "attendance_on_date": t_attendance_on,
    "compare_subjects": t_compare_subjects,
}

TOOL_SPECS = [
    {"type": "function", "function": {
        "name": "class_summary",
        "description": "Overall class attendance, defaulter count and best/weakest subject.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "student_report",
        "description": "Full card for one student: percentage, subject split, trend, projection.",
        "parameters": {"type": "object", "properties": {
            "student": {"type": "string", "description": "roll number or name"}},
            "required": ["student"]}}},
    {"type": "function", "function": {
        "name": "list_defaulters",
        "description": "Students below the cut-off, optionally within one subject.",
        "parameters": {"type": "object", "properties": {
            "subject": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "subject_report",
        "description": "Attendance of one subject, or of all subjects.",
        "parameters": {"type": "object", "properties": {
            "subject": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "classes_needed",
        "description": "How many classes a student must still attend for a target percentage.",
        "parameters": {"type": "object", "properties": {
            "student": {"type": "string"}, "target": {"type": "number"}},
            "required": ["student"]}}},
    {"type": "function", "function": {
        "name": "risk_forecast",
        "description": "Who will finish the semester short, including those sliding.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "detect_anomalies",
        "description": "Mass absence days, sudden drops and unusually weak subjects.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "draft_warning_letters",
        "description": "Write parent warning letters for all defaulters.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "generate_report",
        "description": "Build the PDF and Excel attendance report.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "attendance_on_date",
        "description": "Attendance of one particular date, with the absentee list.",
        "parameters": {"type": "object", "properties": {
            "date": {"type": "string", "description": "YYYY-MM-DD"}},
            "required": ["date"]}}},
    {"type": "function", "function": {
        "name": "compare_subjects",
        "description": "Compare every subject and report the gap.",
        "parameters": {"type": "object", "properties": {}}}},
]

SYSTEM_PROMPT = (
    "You are the supervisor agent inside a college attendance ERP. You never "
    "calculate anything yourself: every figure must come from a tool, and each "
    "tool is owned by a worker agent (AnalyticsAgent owns the numbers, CommsAgent "
    "writes letters, ReportAgent builds files). Call as many tools as the question "
    "needs, then answer in 2-4 short sentences quoting the exact figures returned. "
    "If a tool says something is not possible, say so plainly instead of softening "
    "it. Never invent a student, a subject or a number."
)


# ======================================================================
#  Agent 6 - Supervisor : the orchestrator
#
#  With a Groq key it runs a real function-calling loop: the model sees the
#  eleven tool schemas, picks the tools, the supervisor executes them on the
#  owning agent, feeds the results back, and loops until the model stops
#  calling tools. Without a key the same tools are picked by a keyword
#  planner, so the system degrades instead of dying.
# ======================================================================
class Supervisor:
    name = "Supervisor"
    MAX_STEPS = 4

    def ask(self, ctx, question):
        ctx.log(self.name, "receive question", question[:140], kind="route")
        if not len(ctx.df):
            return "There is no attendance data for your subjects yet.", ctx.trace

        ctx.log("DataAgent", "load scoped frame",
                f"role={ctx.user['role']}",
                f"{len(ctx.df)} records, {ctx.df['Subject'].nunique()} subjects "
                f"visible to this user", kind="result")

        if LLM_.enabled:
            try:
                return self._llm(ctx, question)
            except Exception as e:
                ctx.log(self.name, "LLM unavailable", friendly_error(e),
                        "falling back to the offline planner", kind="route")
        return self._offline(ctx, question)

    # ---- executing one tool on its owning agent ----------------------
    def _run(self, ctx, fn, args):
        if fn not in TOOLS:
            return f"unknown tool {fn}"
        owner = TOOL_OWNER.get(fn, "AnalyticsAgent")
        ctx.log(self.name, f"delegate to {owner}", fn,
                f"tool {fn}({json.dumps(args) if args else ''})", kind="route")
        t0 = time.time()
        try:
            out = str(TOOLS[fn](ctx, **args))
        except TypeError:
            out = str(TOOLS[fn](ctx))
        except Exception as e:
            out = f"tool failed: {e}"
        ctx.log(owner, fn, json.dumps(args) if args else "", out,
                kind="result", ms=int((time.time() - t0) * 1000),
                meta={"tool": fn})
        return out

    # ---- LLM function calling ---------------------------------------
    def _llm(self, ctx, question):
        msgs = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": question}]
        ctx.log(self.name, "plan with LLM", f"groq · {LLM_.model}",
                f"{len(TOOL_SPECS)} tool schemas sent, model decides which to call",
                kind="route")

        for step in range(self.MAX_STEPS):
            msg = LLM_.with_tools(msgs, TOOL_SPECS)["choices"][0]["message"]
            meta = dict(LLM_.last)
            msg.setdefault("content", "")
            msgs.append(msg)
            calls = msg.get("tool_calls") or []

            if (msg.get("content") or "").strip() and calls:
                # the model explained itself before calling a tool - show it
                ctx.log(self.name, "reasoning", "",
                        msg["content"].strip()[:300], kind="think", meta=meta)

            if not calls:
                ctx.used_llm = True
                ctx.log(self.name, "final answer",
                        f"round {step + 1} of {self.MAX_STEPS}",
                        f"{len(ctx.agents_used)} agents used, "
                        f"{meta.get('in_tokens')} in / {meta.get('out_tokens')} out "
                        f"tokens, {meta.get('ms')} ms",
                        kind="step", ms=meta.get("ms"), meta=meta)
                return (msg.get("content") or "").strip(), ctx.trace

            ctx.log(self.name, "model selected tools",
                    ", ".join(c["function"]["name"] for c in calls),
                    f"round {step + 1}, {meta.get('ms')} ms",
                    kind="route", ms=meta.get("ms"), meta=meta)

            for c in calls:
                try:
                    args = json.loads(c["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                msgs.append({"role": "tool", "tool_call_id": c.get("id", ""),
                             "name": c["function"]["name"],
                             "content": self._run(ctx, c["function"]["name"], args)})

        ctx.log(self.name, "stopped", "step limit reached", kind="step")
        return "I could not finish that within the step limit.", ctx.trace

    def _offline(self, ctx, q):
        text = q.lower().strip()
        ctx.log(self.name, "plan without LLM", "keyword planner",
                "no Groq key, matching the question against the tool registry",
                kind="route")
        plan = []

        subject = next((s for s in ctx.df["Subject"].unique()
                        if s.lower() in text), None)
        roll = re.search(r"\b\d{2}[a-z]{2}\d{3}\b", text)
        person = roll.group(0) if roll else next(
            (n for n in ctx.df["Name"].unique()
             if len(n.split()[0]) > 3 and n.split()[0].lower() in text), None)
        date = re.search(r"\d{4}-\d{2}-\d{2}", text)

        if date:
            plan.append(("attendance_on_date", {"date": date.group(0)}))
        if any(w in text for w in ("letter", "mail", "email", "notice", "parent")):
            plan.append(("draft_warning_letters", {}))
        if any(w in text for w in ("report", "pdf", "excel", "download")):
            plan.append(("generate_report", {}))
        if any(w in text for w in ("anomal", "unusual", "strange", "mass", "bunk")):
            plan.append(("detect_anomalies", {}))
        if any(w in text for w in ("predict", "forecast", "risk", "future",
                                   "projected", "end of sem", "will")):
            plan.append(("risk_forecast", {}))
        if person and any(w in text for w in ("how many", "need", "reach", "eligible",
                                              "miss", "bunk", "attend")):
            tgt = re.search(r"(\d{2})\s*%", text)
            plan.append(("classes_needed", {
                "student": person,
                "target": float(tgt.group(1)) if tgt else ctx.threshold}))
        if any(w in text for w in ("defaulter", "below", "short", "less than", "fail")):
            plan.append(("list_defaulters", {"subject": subject} if subject else {}))
        if person and not any(p[0] == "classes_needed" for p in plan):
            plan.append(("student_report", {"student": person}))
        if subject and not any(p[0] == "list_defaulters" for p in plan):
            plan.append(("subject_report", {"subject": subject}))
        if any(w in text for w in ("compare", "which subject", "best subject",
                                   "worst subject")):
            plan.append(("compare_subjects", {}))
        if not plan:
            plan.append(("class_summary", {}))

        seen, final = set(), []
        for fn, a in plan:
            if fn not in seen:
                seen.add(fn)
                final.append((fn, a))
        ctx.log(self.name, "planner selected tools",
                ", ".join(f for f, _ in final),
                "agents to activate: " + ", ".join(
                    sorted({TOOL_OWNER.get(f, "AnalyticsAgent") for f, _ in final})),
                kind="route")
        answers = [self._run(ctx, fn, a) for fn, a in final]
        ctx.log(self.name, "final answer", f"{len(final)} tool(s) used",
                f"{len(ctx.agents_used)} agents activated", kind="step")
        return "\n\n".join(answers), ctx.trace


SUP = Supervisor()




# ======================================================================
#  Agent evaluation
#  Twenty fixed questions with the tools a correct answer must use. The
#  suite runs against either planner, so "is the LLM actually better at
#  routing than keywords" stops being an opinion.
# ======================================================================
EVAL_CASES = [
    {"q": "Give me the class summary", "expect": ["class_summary"]},
    {"q": "What is the overall attendance of the class?", "expect": ["class_summary"]},
    {"q": "Who are the defaulters?", "expect": ["list_defaulters"]},
    {"q": "List the students below the cut-off in Soft Skills",
     "expect": ["list_defaulters"]},
    {"q": "Tell me about 23CS104", "expect": ["student_report"]},
    {"q": "How is Sneha Iyer doing?", "expect": ["student_report"]},
    {"q": "How many classes does 23CS111 need to reach 75%?",
     "expect": ["classes_needed"]},
    {"q": "How many more classes can 23CS103 miss?", "expect": ["classes_needed"]},
    {"q": "Who is at risk of falling short by the end of the semester?",
     "expect": ["risk_forecast"]},
    {"q": "Predict who will be short in November", "expect": ["risk_forecast"]},
    {"q": "Is there anything unusual in this data?", "expect": ["detect_anomalies"]},
    {"q": "Was there a day when the whole class bunked?",
     "expect": ["detect_anomalies"]},
    {"q": "What happened on 2026-08-18?", "expect": ["attendance_on_date"]},
    {"q": "Show me attendance for 2026-07-30", "expect": ["attendance_on_date"]},
    {"q": "Which subject has the worst attendance?",
     "expect": ["compare_subjects", "subject_report"], "any": True},
    {"q": "Compare all the subjects", "expect": ["compare_subjects"]},
    {"q": "How is DBMS doing?", "expect": ["subject_report"]},
    {"q": "Draft warning letters for the defaulters",
     "expect": ["draft_warning_letters"]},
    {"q": "Write to the parents of students who are short",
     "expect": ["draft_warning_letters"]},
    {"q": "Generate the attendance report", "expect": ["generate_report"]},

    # the hard half: nothing here contains the obvious keyword, so the
    # fallback planner is expected to struggle. This is the honest part of
    # the evaluation - it shows what the LLM router is actually buying.
    {"q": "Should I be worried about anyone in this class?",
     "expect": ["risk_forecast"], "hard": True},
    {"q": "Sneha has been ill for a while, can she still make it?",
     "expect": ["classes_needed", "student_report"], "any": True, "hard": True},
    {"q": "Send a note home for anyone who is struggling",
     "expect": ["draft_warning_letters"], "hard": True},
    {"q": "Give me everything I need for the HOD meeting tomorrow",
     "expect": ["generate_report"], "hard": True},
    {"q": "Is one subject dragging the class down?",
     "expect": ["compare_subjects", "subject_report"], "any": True, "hard": True},
    {"q": "Which of my students are heading the wrong way?",
     "expect": ["risk_forecast"], "hard": True},
]


def run_eval(user, threshold=THRESHOLD_DEFAULT, use_llm=False, cases=None):
    cases = cases or EVAL_CASES
    was = LLM_.key
    if not use_llm:
        LLM_.key = ""                      # force the keyword planner
    rows, hits, t0 = [], 0, time.time()
    try:
        for c in cases:
            ctx = Ctx(user, threshold)
            try:
                SUP.ask(ctx, c["q"])
                called = [t["meta"].get("tool") for t in ctx.trace
                          if t["kind"] == "result" and t["meta"].get("tool")]
            except Exception as e:
                called = [f"error: {e}"]
            want = c["expect"]
            ok = (any(w in called for w in want) if c.get("any")
                  else all(w in called for w in want))
            hits += bool(ok)
            rows.append({"question": c["q"], "expected": want, "called": called,
                         "pass": bool(ok), "hard": bool(c.get("hard")),
                         "agents": ctx.agents_used})
    finally:
        LLM_.key = was
    easy = [r for r in rows if not r["hard"]]
    hard = [r for r in rows if r["hard"]]
    pct = lambda rs: round(sum(r["pass"] for r in rs) / max(len(rs), 1) * 100, 1)
    return {"planner": "llm" if use_llm else "keyword",
            "passed": hits, "total": len(cases),
            "accuracy": round(hits / len(cases) * 100, 1),
            "plain": {"passed": sum(r["pass"] for r in easy), "total": len(easy),
                      "accuracy": pct(easy)},
            "hard": {"passed": sum(r["pass"] for r in hard), "total": len(hard),
                     "accuracy": pct(hard)},
            "seconds": round(time.time() - t0, 2), "rows": rows}


# ======================================================================
#  FastAPI
# ======================================================================
app = FastAPI(title="AttendX API")
seed_if_empty()


class LoginIn(BaseModel):
    username: str
    password: str


class AskIn(BaseModel):
    question: str
    threshold: int = THRESHOLD_DEFAULT
    api_key: str = ""


class MarkIn(BaseModel):
    date: str
    subject_id: int
    entries: list          # [{"student_id": 1, "present": 1}, ...]


class StudentIn(BaseModel):
    roll: str
    name: str
    email: str = ""


class SubjectIn(BaseModel):
    code: str
    name: str
    teacher_id: int


class ExemptionIn(BaseModel):
    student_id: int
    subject_id: int | None = None
    from_date: str
    to_date: str
    reason: str
    kind: str = "medical"


class SendIn(BaseModel):
    ids: list = []
    dry_run: bool = True
    host: str = ""
    port: int = 587
    user: str = ""
    password: str = ""


class EvalIn(BaseModel):
    threshold: int = THRESHOLD_DEFAULT
    use_llm: bool = False
    api_key: str = ""


def frame_for(user, threshold=THRESHOLD_DEFAULT):
    df = DATA.frame(teacher=user)
    if not len(df):
        raise HTTPException(400, "no attendance data available for your subjects")
    return df


@app.post("/api/login")
def api_login(body: LoginIn):
    out = login_user(body.username, body.password)
    if not out:
        raise HTTPException(401, "wrong username or password")
    return out


@app.post("/api/logout")
def api_logout(user=Depends(current_user), authorization: str = Header(default="")):
    with closing(db()) as con:
        con.execute("DELETE FROM sessions WHERE token=?",
                    (authorization.replace("Bearer", "").strip(),))
        con.commit()
    audit(user["username"], "Auth", "logout")
    return {"ok": True}


@app.get("/api/me")
def api_me(user=Depends(current_user)):
    return {**user, "llm": LLM_.enabled, "model": GROQ_MODEL,
            "classes_left": CLASSES_LEFT}


@app.get("/api/dashboard")
def api_dashboard(threshold: int = THRESHOLD_DEFAULT, user=Depends(current_user)):
    df = frame_for(user)
    s = AN.stats(df, threshold)
    raw = DATA.frame(teacher=user, include_exempt=True)
    s["exempted"] = int(len(raw) - len(df))
    return {
        "stats": s,
        "students": AN.student_table(df, threshold).to_dict("records"),
        "subjects": AN.subject_table(df).to_dict("records"),
        "daily": AN.daily(df).to_dict("records"),
        "forecast": AN.forecast(df, threshold).to_dict("records"),
        "anomalies": AN.anomalies(df, threshold).to_dict("records"),
        "heatmap": {"index": AN.heatmap(df).index.tolist(),
                    "columns": AN.heatmap(df).columns.tolist(),
                    "values": AN.heatmap(df).fillna(0).values.tolist()},
        "narrative": INSIGHT.summary(df, threshold, user["username"]),
    }


@app.get("/api/students")
def api_students(user=Depends(current_user)):
    with closing(db()) as con:
        return [dict(r) for r in con.execute(
            "SELECT * FROM students ORDER BY roll")]


@app.post("/api/students")
def api_add_student(body: StudentIn, user=Depends(current_user)):
    if user["role"] != "hod":
        raise HTTPException(403, "only the HOD can manage students")
    with closing(db()) as con:
        try:
            con.execute("INSERT INTO students (roll,name,email) VALUES (?,?,?)",
                        (body.roll.strip(), body.name.strip(), body.email.strip()))
            con.commit()
        except sqlite3.IntegrityError:
            raise HTTPException(400, "that roll number already exists")
    audit(user["username"], "ERP", "add_student", body.roll)
    return {"ok": True}


@app.delete("/api/students/{sid}")
def api_del_student(sid: int, user=Depends(current_user)):
    if user["role"] != "hod":
        raise HTTPException(403, "only the HOD can manage students")
    with closing(db()) as con:
        con.execute("UPDATE students SET active=0 WHERE id=?", (sid,))
        con.commit()
    audit(user["username"], "ERP", "deactivate_student", sid)
    return {"ok": True}


@app.get("/api/subjects")
def api_subjects(user=Depends(current_user)):
    with closing(db()) as con:
        rows = con.execute(
            "SELECT s.*, t.name AS teacher FROM subjects s "
            "LEFT JOIN teachers t ON t.id = s.teacher_id ORDER BY s.code").fetchall()
    out = [dict(r) for r in rows]
    if user["role"] == "teacher":
        out = [r for r in out if r["teacher_id"] == user["id"]]
    return out


@app.post("/api/subjects")
def api_add_subject(body: SubjectIn, user=Depends(current_user)):
    if user["role"] != "hod":
        raise HTTPException(403, "only the HOD can manage subjects")
    with closing(db()) as con:
        try:
            con.execute("INSERT INTO subjects (code,name,teacher_id) VALUES (?,?,?)",
                        (body.code.strip(), body.name.strip(), body.teacher_id))
            con.commit()
        except sqlite3.IntegrityError:
            raise HTTPException(400, "that subject code already exists")
    audit(user["username"], "ERP", "add_subject", body.code)
    return {"ok": True}


@app.get("/api/teachers")
def api_teachers(user=Depends(current_user)):
    with closing(db()) as con:
        return [{"id": r["id"], "name": r["name"], "role": r["role"]}
                for r in con.execute("SELECT id,name,role FROM teachers")]


@app.get("/api/roster")
def api_roster(subject_id: int, date: str, user=Depends(current_user)):
    """the class list for one subject on one date, with whatever is already marked"""
    with closing(db()) as con:
        owner = con.execute("SELECT teacher_id FROM subjects WHERE id=?",
                            (subject_id,)).fetchone()
        if not owner:
            raise HTTPException(404, "no such subject")
        if user["role"] == "teacher" and owner["teacher_id"] != user["id"]:
            raise HTTPException(403, "that subject is not allotted to you")
        rows = con.execute(
            "SELECT st.id, st.roll, st.name, a.present "
            "FROM students st LEFT JOIN attendance a "
            "  ON a.student_id = st.id AND a.subject_id = ? AND a.date = ? "
            "WHERE st.active = 1 ORDER BY st.roll", (subject_id, date)).fetchall()
    return [{"student_id": r["id"], "roll": r["roll"], "name": r["name"],
             "present": 1 if r["present"] is None else r["present"],
             "already_marked": r["present"] is not None} for r in rows]


@app.post("/api/attendance")
def api_mark(body: MarkIn, user=Depends(current_user)):
    with closing(db()) as con:
        owner = con.execute("SELECT teacher_id, name FROM subjects WHERE id=?",
                            (body.subject_id,)).fetchone()
        if not owner:
            raise HTTPException(404, "no such subject")
        if user["role"] == "teacher" and owner["teacher_id"] != user["id"]:
            raise HTTPException(403, "that subject is not allotted to you")
        now = dt.datetime.now().isoformat(timespec="seconds")
        con.executemany(
            "INSERT OR REPLACE INTO attendance "
            "(date,subject_id,student_id,present,marked_by,marked_at) "
            "VALUES (?,?,?,?,?,?)",
            [(body.date, body.subject_id, int(e["student_id"]),
              1 if e.get("present") else 0, user["id"], now) for e in body.entries])
        con.commit()
    present = sum(1 for e in body.entries if e.get("present"))
    audit(user["username"], "ERP", "mark_attendance",
          f"{owner['name']} {body.date}",
          f"{present}/{len(body.entries)} present")
    return {"ok": True, "present": present, "total": len(body.entries)}


@app.post("/api/import")
async def api_import(file: UploadFile = File(...), user=Depends(current_user)):
    path = os.path.join(OUT_DIR, "_upload_" + os.path.basename(file.filename))
    with open(path, "wb") as fh:
        fh.write(await file.read())
    try:
        return DATA.import_csv(path, user["username"])
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post("/api/agent/ask")
def api_ask(body: AskIn, user=Depends(current_user)):
    if body.api_key:
        LLM_.key = body.api_key
    ctx = Ctx(user, body.threshold)
    answer, trace = SUP.ask(ctx, body.question)
    tools = [t["meta"].get("tool") for t in trace
             if t["kind"] == "result" and t["meta"].get("tool")]
    llm_steps = [t for t in trace if t.get("meta", {}).get("model")]
    return {
        "answer": answer,
        "trace": trace,
        "agents_used": ctx.agents_used,
        "tools_used": tools,
        "planner": (f"groq · {LLM_.model}" if ctx.used_llm else
                    ("offline planner (LLM unavailable)" if LLM_.enabled
                     else "offline planner")),
        "llm": ctx.used_llm,
        "rounds": len(llm_steps),
        "tokens": {
            "in": sum(t["meta"].get("in_tokens") or 0 for t in llm_steps),
            "out": sum(t["meta"].get("out_tokens") or 0 for t in llm_steps),
        },
        "ms": int((time.time() - ctx.t0) * 1000),
    }


@app.get("/api/agents")
def api_agents(user=Depends(current_user)):
    """the registry plus how often each agent has actually run"""
    with closing(db()) as con:
        counts = {r["agent"]: r["n"] for r in con.execute(
            "SELECT agent, COUNT(*) n FROM audit GROUP BY agent")}
    out = []
    for name, spec in AGENT_SPECS.items():
        out.append({"name": name, **spec, "calls": counts.get(name, 0),
                    "tool_count": len(spec["tools"])})
    return {"agents": out,
            "tools": [{"name": t["function"]["name"],
                       "description": t["function"]["description"],
                       "owner": TOOL_OWNER.get(t["function"]["name"], "AnalyticsAgent"),
                       "params": list((t["function"].get("parameters") or {})
                                      .get("properties", {}).keys())}
                      for t in TOOL_SPECS],
            "llm": {"enabled": LLM_.enabled, "model": LLM_.model,
                    "candidates": LLM.MODELS},
            "system_prompt": SYSTEM_PROMPT}


@app.post("/api/agent/test")
def api_agent_test(body: AskIn, user=Depends(current_user)):
    """the 'test key' button: proves the Groq key works and picks a live model"""
    res = LLM_.check(body.api_key or LLM_.key)
    audit(user["username"], "Supervisor", "test_llm_key",
          res.get("model", ""), "ok" if res.get("ok") else res.get("error", "")[:120])
    return res


@app.post("/api/letters")
def api_letters(threshold: int = THRESHOLD_DEFAULT, user=Depends(current_user)):
    return {"letters": COMMS.draft(frame_for(user), threshold, user["username"])}


@app.post("/api/report")
def api_report(threshold: int = THRESHOLD_DEFAULT, user=Depends(current_user)):
    df = frame_for(user)
    narrative = INSIGHT.summary(df, threshold, user["username"])
    return REPORT.build(df, threshold, narrative, user["username"])


@app.get("/api/download/{name}")
def api_download(name: str, user=Depends(current_user)):
    path = os.path.join(OUT_DIR, os.path.basename(name))
    if not os.path.exists(path):
        raise HTTPException(404, "file not generated yet")
    return FileResponse(path, filename=os.path.basename(path))


@app.get("/api/audit")
def api_audit(limit: int = 200, user=Depends(current_user)):
    with closing(db()) as con:
        return [dict(r) for r in con.execute(
            "SELECT ts, actor, agent, action, params, outcome FROM audit "
            "ORDER BY id DESC LIMIT ?", (limit,))]




# ---------- exemptions ---------------------------------------------------
@app.get("/api/exemptions")
def api_exemptions(user=Depends(current_user)):
    with closing(db()) as con:
        rows = con.execute(
            "SELECT e.*, st.roll, st.name AS student, sub.name AS subject, "
            "t.name AS approver FROM exemptions e "
            "JOIN students st ON st.id = e.student_id "
            "LEFT JOIN subjects sub ON sub.id = e.subject_id "
            "LEFT JOIN teachers t ON t.id = e.approved_by "
            "ORDER BY e.from_date DESC").fetchall()
    out = [dict(r) for r in rows]
    if user["role"] == "student":
        out = [r for r in out if r["student_id"] == user.get("student_id")]
    return out


@app.post("/api/exemptions")
def api_add_exemption(body: ExemptionIn, user=Depends(current_user)):
    if user["role"] == "student":
        raise HTTPException(403, "students cannot approve their own exemptions")
    if body.to_date < body.from_date:
        raise HTTPException(400, "the end date is before the start date")
    with closing(db()) as con:
        con.execute("INSERT INTO exemptions (student_id,subject_id,from_date,to_date,"
                    "reason,kind,approved_by,created) VALUES (?,?,?,?,?,?,?,?)",
                    (body.student_id, body.subject_id, body.from_date, body.to_date,
                     body.reason.strip(), body.kind, user["id"],
                     dt.datetime.now().isoformat(timespec="seconds")))
        con.commit()
    audit(user["username"], "ERP", "approve_exemption",
          f"student {body.student_id} {body.from_date}..{body.to_date}", body.kind)
    return {"ok": True}


@app.delete("/api/exemptions/{eid}")
def api_del_exemption(eid: int, user=Depends(current_user)):
    if user["role"] == "student":
        raise HTTPException(403, "not allowed")
    with closing(db()) as con:
        con.execute("DELETE FROM exemptions WHERE id=?", (eid,))
        con.commit()
    audit(user["username"], "ERP", "revoke_exemption", eid)
    return {"ok": True}


# ---------- letter delivery ---------------------------------------------
@app.get("/api/letters")
def api_letters_list(user=Depends(current_user)):
    with closing(db()) as con:
        return [dict(r) for r in con.execute(
            "SELECT id, roll, name, percent, must_attend, file, status, body, "
            "drafted_at, sent_at, sent_to, note FROM letters ORDER BY percent")]


@app.post("/api/letters/send")
def api_letters_send(body: SendIn, user=Depends(current_user)):
    if user["role"] == "student":
        raise HTTPException(403, "not allowed")
    try:
        return COMMS.send(body.ids, user["username"], dry_run=body.dry_run,
                          smtp={"host": body.host, "port": body.port,
                                "user": body.user, "password": body.password})
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(400, f"sending failed: {e}")


# ---------- the student's own view --------------------------------------
@app.get("/api/student/me")
def api_student_me(threshold: int = THRESHOLD_DEFAULT, user=Depends(current_user)):
    if user["role"] != "student":
        raise HTTPException(403, "this view is for students")
    df = DATA.frame(teacher=user)
    if not len(df):
        raise HTTPException(400, "no attendance recorded for you yet")
    st = AN.student_table(df, threshold).iloc[0]
    fc = AN.forecast(df, threshold).iloc[0]
    subs = (df.groupby("Subject")
              .agg(Classes=("Present", "size"), Attended=("Present", "sum"))
              .reset_index())
    subs["Percent"] = (subs["Attended"] / subs["Classes"] * 100).round(2)
    with closing(db()) as con:
        ex = [dict(r) for r in con.execute(
            "SELECT e.from_date, e.to_date, e.reason, e.kind FROM exemptions e "
            "WHERE e.student_id = ? ORDER BY e.from_date DESC",
            (user.get("student_id") or -1,))]
    return {"card": {k: (v.item() if hasattr(v, "item") else v)
                     for k, v in st.to_dict().items()},
            "forecast": {k: (v.item() if hasattr(v, "item") else v)
                         for k, v in fc.to_dict().items()},
            "subjects": subs.to_dict("records"),
            "daily": AN.daily(df).to_dict("records"),
            "exemptions": ex, "threshold": threshold,
            "classes_left": CLASSES_LEFT}


# ---------- agent evaluation --------------------------------------------
@app.post("/api/agent/eval")
def api_eval(body: EvalIn, user=Depends(current_user)):
    if user["role"] == "student":
        raise HTTPException(403, "not allowed")
    if body.api_key:
        LLM_.key = body.api_key
    if body.use_llm and not LLM_.enabled:
        raise HTTPException(400, "no Groq key set, cannot evaluate the LLM planner")
    out = run_eval(user, body.threshold, use_llm=body.use_llm)
    audit(user["username"], "Supervisor", "run_eval", out["planner"],
          f"{out['passed']}/{out['total']} ({out['accuracy']}%)")
    return out


@app.get("/api/eval/cases")
def api_eval_cases(user=Depends(current_user)):
    return EVAL_CASES


@app.get("/")
def index():
    if not os.path.exists(FRONTEND):
        return JSONResponse({"error": "frontend/index.html is missing"}, 500)
    return FileResponse(FRONTEND)


VENDOR = os.path.join(BASE, "frontend", "vendor")
if os.path.isdir(VENDOR):
    # react, chart.js and tailwind are served from disk, not from a CDN,
    # so the ERP still runs on a laptop with no internet
    app.mount("/vendor", StaticFiles(directory=VENDOR), name="vendor")


# ======================================================================
#  Gradio console, mounted inside the same FastAPI app at /agent
#  (the React ERP embeds this page on its "Agent console" screen)
# ======================================================================
def gradio_console():
    admin = {"id": 2, "username": "console", "name": "Gradio console", "role": "hod"}

    def ask(message, history, key, threshold):
        if key:
            LLM_.key = key
        if not message.strip():
            return history, ""
        ctx = Ctx(admin, int(threshold))
        answer, trace = SUP.ask(ctx, message)
        lines = [f"[{t['ts']}] {t['agent']:<14} | {t['action']}"
                 + (f"  ({t['params']})" if t["params"] else "")
                 + (f"\n{'':<18}-> {t['outcome']}" if t["outcome"] else "")
                 for t in trace]
        history = (history or []) + [
            {"role": "user", "content": message},
            {"role": "assistant", "content": answer}]
        return history, "\n".join(lines)

    def build(component, **kw):
        # gradio renames arguments between versions, drop what is not supported
        while True:
            try:
                return component(**kw)
            except TypeError as e:
                bad = re.search(r"unexpected keyword argument '(\w+)'", str(e))
                if not bad or bad.group(1) not in kw:
                    raise
                kw.pop(bad.group(1))

    css = """
    .gradio-container{background:#0b0f17!important;color:#e6edf7!important}
    #trace textarea{background:#070b12!important;color:#7ef2c4!important;
      font-family:ui-monospace,monospace!important;font-size:11.5px!important}
    """
    with build(gr.Blocks, theme=gr.themes.Base(primary_hue="cyan",
                                               neutral_hue="slate"), css=css) as demo:
        gr.Markdown("### Agent console\nThe same supervisor and the same eleven "
                    "tools the ERP uses, exposed as a raw Gradio console for "
                    "debugging and demos.")
        with gr.Row():
            key = gr.Textbox(label="Groq API key (optional)", type="password", scale=3)
            th = gr.Slider(50, 90, value=75, step=1, label="cut-off %", scale=2)
        chat = build(gr.Chatbot, type="messages", height=330, show_label=False)
        with gr.Row():
            msg = gr.Textbox(placeholder="ask the attendance agent...", scale=5,
                             show_label=False, container=False)
            send = gr.Button("Send", variant="primary", scale=1)
        trace = build(gr.Textbox, label="tool trace", lines=12, elem_id="trace")
        gr.Examples(["Give me the class summary",
                     "Who are the defaulters in Soft Skills?",
                     "How many classes does 23CS104 need to reach 75%?",
                     "Who is at risk by the end of the semester?",
                     "Is there anything unusual in this data?"], inputs=msg, label="")
        send.click(ask, [msg, chat, key, th], [chat, trace]).then(lambda: "", None, msg)
        msg.submit(ask, [msg, chat, key, th], [chat, trace]).then(lambda: "", None, msg)
    return demo


app = gr.mount_gradio_app(app, gradio_console(), path="/agent")


if __name__ == "__main__":
    # a hosting provider hands the port in $PORT and needs 0.0.0.0;
    # on a laptop it stays on localhost
    port = int(os.environ.get("PORT", 8000))
    host = os.environ.get("HOST", "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1")
    print(f"\n  AttendX running on http://{host}:{port}")
    print("  logins:  ishika / teach123     hod / admin123\n")
    uvicorn.run(app, host=host, port=port, log_level="warning")
