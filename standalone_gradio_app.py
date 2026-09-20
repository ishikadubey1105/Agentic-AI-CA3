"""
Attendance Intelligence System
Agentic AI and Automation - Unit Test 3 project

A multi-agent system that ingests attendance data, analyses it, predicts who is
going to fall short, detects unusual patterns, drafts warning mails and writes
the final report - and a supervisor agent that decides which of these tools to
call based on a question asked in plain English.

Run:
    pip install gradio pandas numpy matplotlib requests openpyxl
    python app.py

Opens on http://127.0.0.1:7860

Works fully offline (keyword based planner). Paste a Groq API key in the
sidebar and the same tools get driven by an LLM using function calling.
"""

import os
import re
import json
import time
import random
import sqlite3
import zipfile
import datetime as dt

import numpy as np
import pandas as pd

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages

import requests
import gradio as gr


OUT_DIR = "outputs"
MAIL_DIR = os.path.join(OUT_DIR, "emails")
DB_PATH = os.path.join(OUT_DIR, "agent_memory.db")
os.makedirs(MAIL_DIR, exist_ok=True)

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.3-70b-versatile"

REQUIRED_COLS = ["Date", "RollNo", "Name", "Subject", "Status"]

# how many more classes are left in the semester - used by the forecasting tool
CLASSES_LEFT = 40

# dark "command centre" palette, shared by the charts, the PDF and the web UI
INK = "#0b0f17"          # page background
PANEL = "#121a27"        # card background
LINE = "#223049"         # borders and grid
TEXT = "#e6edf7"
MUTED = "#8fa3bf"
CYAN = "#22d3ee"
VIOLET = "#a78bfa"
GREEN = "#34d399"
AMBER = "#fbbf24"
RED = "#f87171"
BLUE = CYAN
GREY = MUTED

plt.rcParams.update({
    "figure.facecolor": PANEL,
    "savefig.facecolor": PANEL,
    "axes.facecolor": PANEL,
    "axes.edgecolor": LINE,
    "axes.labelcolor": MUTED,
    "text.color": TEXT,
    "xtick.color": MUTED,
    "ytick.color": MUTED,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "axes.grid": True,
    "grid.color": LINE,
    "grid.linewidth": 0.7,
    "axes.axisbelow": True,
    "legend.facecolor": PANEL,
    "legend.edgecolor": LINE,
    "font.size": 9,
})


# ======================================================================
#  Memory - every agent action is written to sqlite so the run is auditable
# ======================================================================
class Memory:
    def __init__(self, path=DB_PATH):
        self.con = sqlite3.connect(path, check_same_thread=False)
        self.con.execute("""CREATE TABLE IF NOT EXISTS actions (
                                id INTEGER PRIMARY KEY AUTOINCREMENT,
                                ts TEXT, agent TEXT, action TEXT,
                                params TEXT, outcome TEXT)""")
        self.con.commit()
        self.trace = []                      # live log for the current request

    def log(self, agent, action, params="", outcome=""):
        ts = dt.datetime.now().strftime("%H:%M:%S")
        self.con.execute("INSERT INTO actions (ts, agent, action, params, outcome)"
                         " VALUES (?,?,?,?,?)",
                         (dt.datetime.now().isoformat(timespec="seconds"),
                          agent, action, str(params)[:400], str(outcome)[:400]))
        self.con.commit()
        line = f"[{ts}] {agent:<16} | {action}"
        if params:
            line += f"  ({params})"
        if outcome:
            line += f"\n{'':<20}-> {outcome}"
        self.trace.append(line)

    def new_trace(self):
        self.trace = []

    def trace_text(self):
        return "\n".join(self.trace)

    def audit(self, limit=200):
        return pd.read_sql_query(
            f"SELECT ts, agent, action, params, outcome FROM actions "
            f"ORDER BY id DESC LIMIT {limit}", self.con)


MEM = Memory()


# ======================================================================
#  Shared state - the agents' working memory
# ======================================================================
class State:
    def __init__(self):
        self.df = None                # cleaned attendance records
        self.threshold = 75
        self.source = None
        self.charts = []
        self.reports = []

    @property
    def loaded(self):
        return self.df is not None and len(self.df)


ST = State()


# ======================================================================
#  Agent 1 - Data agent : ingest, clean, profile
# ======================================================================
class DataAgent:
    name = "DataAgent"

    def load(self, path):
        MEM.log(self.name, "load_file", os.path.basename(path))

        if path.lower().endswith((".xlsx", ".xls")):
            df = pd.read_excel(path)
        else:
            df = pd.read_csv(path)

        # column names come in every possible spelling, map them first
        alias = {
            "date": "Date", "attendancedate": "Date", "classdate": "Date",
            "rollno": "RollNo", "roll": "RollNo", "rollnumber": "RollNo",
            "prn": "RollNo", "id": "RollNo", "studentid": "RollNo",
            "name": "Name", "studentname": "Name",
            "subject": "Subject", "course": "Subject", "sub": "Subject",
            "status": "Status", "attendance": "Status", "present": "Status",
        }
        ren = {}
        for c in df.columns:
            k = re.sub(r"[^a-z]", "", str(c).lower())
            if k in alias:
                ren[c] = alias[k]
        df = df.rename(columns=ren)

        missing = [c for c in REQUIRED_COLS if c not in df.columns]
        if missing:
            raise ValueError("Missing column(s): " + ", ".join(missing) +
                             ". Expected: Date, RollNo, Name, Subject, Status")

        raw = len(df)
        df = df[REQUIRED_COLS].copy()

        # dates: try the standard format, if it mostly fails assume dd-mm-yyyy
        parsed = pd.to_datetime(df["Date"], errors="coerce")
        if parsed.isna().mean() > 0.3:
            parsed = pd.to_datetime(df["Date"], errors="coerce", dayfirst=True)
        df["Date"] = parsed

        df["Status"] = (df["Status"].astype(str).str.strip().str.upper()
                        .replace({"P": "PRESENT", "A": "ABSENT", "1": "PRESENT",
                                  "0": "ABSENT", "TRUE": "PRESENT",
                                  "FALSE": "ABSENT", "YES": "PRESENT",
                                  "NO": "ABSENT", "PRESENT": "PRESENT",
                                  "ABSENT": "ABSENT"}))

        bad_status = int((~df["Status"].isin(["PRESENT", "ABSENT"])).sum())
        df = df[df["Status"].isin(["PRESENT", "ABSENT"])]
        bad_date = int(df["Date"].isna().sum())
        df = df.dropna(subset=["Date", "RollNo", "Subject"])
        dupes = int(df.duplicated(subset=["Date", "RollNo", "Subject"]).sum())
        df = df.drop_duplicates(subset=["Date", "RollNo", "Subject"])

        df["RollNo"] = df["RollNo"].astype(str).str.strip()
        df["Name"] = df["Name"].astype(str).str.strip()
        df["Subject"] = df["Subject"].astype(str).str.strip()
        df["Present"] = (df["Status"] == "PRESENT").astype(int)

        ST.df, ST.source = df, os.path.basename(path)
        quality = (f"{raw} rows read, {len(df)} kept "
                   f"(dropped {bad_status} bad status, {bad_date} bad dates, "
                   f"{dupes} duplicates)")
        MEM.log(self.name, "clean", "", quality)
        MEM.log(self.name, "profile", "",
                f"{df['RollNo'].nunique()} students, {df['Subject'].nunique()} subjects, "
                f"{df['Date'].dt.date.nunique()} class days")
        return quality


# ======================================================================
#  Agent 2 - Analytics agent : the maths lives here
# ======================================================================
class AnalyticsAgent:
    name = "AnalyticsAgent"

    def __init__(self):
        self._fc = None          # forecast cache, it is the costly one
        self._fc_key = None

    def student_table(self, subject=None):
        df = ST.df if not subject else ST.df[ST.df["Subject"].str.lower() == subject.lower()]
        t = (df.groupby(["RollNo", "Name"])
               .agg(Classes=("Present", "size"), Attended=("Present", "sum"))
               .reset_index())
        t["Percent"] = (t["Attended"] / t["Classes"] * 100).round(2)
        th = ST.threshold
        t["Remark"] = t["Percent"].apply(
            lambda p: "Defaulter" if p < th else ("Borderline" if p < th + 8 else "Safe"))
        return t.sort_values("Percent").reset_index(drop=True)

    def subject_table(self):
        t = (ST.df.groupby("Subject")
                  .agg(Classes=("Present", "size"), Attended=("Present", "sum"))
                  .reset_index())
        t["Percent"] = (t["Attended"] / t["Classes"] * 100).round(2)
        return t.sort_values("Percent").reset_index(drop=True)

    def daily(self):
        d = (ST.df.groupby(ST.df["Date"].dt.date)["Present"].mean() * 100).round(2)
        d = d.reset_index()
        d.columns = ["Date", "Percent"]
        return d

    def pivot(self):
        """student x subject matrix, used for the heatmap"""
        p = (ST.df.pivot_table(index="Name", columns="Subject",
                               values="Present", aggfunc="mean") * 100).round(1)
        return p

    # ---- prediction -------------------------------------------------
    @staticmethod
    def classes_needed(attended, total, target):
        """minimum consecutive classes a student must attend to reach target %"""
        if total and attended / total * 100 >= target:
            return 0
        need = (target * total - 100 * attended) / (100 - target)
        return int(np.ceil(max(need, 0)))

    @staticmethod
    def best_possible(attended, total):
        """highest percentage a student can still reach if they never miss again"""
        return round((attended + CLASSES_LEFT) / (total + CLASSES_LEFT) * 100, 2)

    @staticmethod
    def safe_bunks(attended, total, target):
        """how many more classes can be missed while staying above target"""
        if total and attended / total * 100 < target:
            return 0
        n = (100 * attended - target * total) / target
        return int(np.floor(max(n, 0)))

    def forecast(self):
        """
        Fit a straight line through each student's weekly attendance and project
        it forward over the classes still left in the semester. Gives an early
        warning even for students who are above the cut-off today but sliding.
        """
        key = (id(ST.df), len(ST.df), ST.threshold)
        if self._fc_key == key and self._fc is not None:
            return self._fc.copy()

        df = ST.df.copy()
        df["Week"] = df["Date"].dt.isocalendar().week.astype(int)
        rows = []
        for (roll, name), g in df.groupby(["RollNo", "Name"]):
            weekly = g.groupby("Week")["Present"].mean() * 100
            cur = g["Present"].mean() * 100
            if len(weekly) >= 3:
                slope = float(np.polyfit(np.arange(len(weekly)), weekly.values, 1)[0])
            else:
                slope = 0.0
            # expected rate over the remaining classes, clipped to a sane range
            future_rate = float(np.clip(weekly.iloc[-1] + slope * 2, 0, 100))
            att, tot = int(g["Present"].sum()), int(len(g))
            projected = (att + future_rate / 100 * CLASSES_LEFT) / (tot + CLASSES_LEFT) * 100
            rows.append({
                "RollNo": roll, "Name": name,
                "Current": round(cur, 2),
                "Trend": round(slope, 2),
                "Projected": round(projected, 2),
                "NeedToAttend": self.classes_needed(att, tot, ST.threshold),
                "CanMiss": self.safe_bunks(att, tot, ST.threshold),
                "BestPossible": self.best_possible(att, tot),
            })
        t = pd.DataFrame(rows)
        th = ST.threshold
        # a student whose best possible score is still short cannot recover at all
        t["Recoverable"] = np.where(t["BestPossible"] < th, "not possible",
                           np.where(t["NeedToAttend"] > 0, "yes, no more absence", "safe"))
        t["Risk"] = np.where(t["Projected"] < th, "High",
                    np.where((t["Current"] >= th) & (t["Trend"] < -1.5), "Watch",
                    np.where(t["Projected"] < th + 6, "Medium", "Low")))
        order = {"High": 0, "Watch": 1, "Medium": 2, "Low": 3}
        t = t.sort_values(["Risk", "Projected"], key=lambda c: c.map(order).fillna(c))
        MEM.log(self.name, "forecast",
                f"{CLASSES_LEFT} classes left",
                f"{(t['Risk'] == 'High').sum()} high risk, "
                f"{(t['Risk'] == 'Watch').sum()} sliding but still safe")
        t = t.reset_index(drop=True)
        self._fc, self._fc_key = t, key
        return t.copy()

    # ---- anomaly detection ------------------------------------------
    def anomalies(self):
        out = []
        d = self.daily()

        # 1. mass bunk days - more than 2 std below the usual day
        mu, sd = d["Percent"].mean(), d["Percent"].std()
        for _, r in d[d["Percent"] < mu - 2 * sd].iterrows():
            out.append({"Type": "Mass absence", "Where": str(r["Date"]),
                        "Detail": f"only {r['Percent']}% present, class average is {mu:.1f}%"})

        # 2. students who dropped sharply in the last two weeks
        last = ST.df["Date"].max() - pd.Timedelta(days=14)
        for (roll, name), g in ST.df.groupby(["RollNo", "Name"]):
            recent, older = g[g["Date"] > last], g[g["Date"] <= last]
            if len(recent) >= 5 and len(older) >= 5:
                rp, op = recent["Present"].mean() * 100, older["Present"].mean() * 100
                if op - rp >= 20:
                    out.append({"Type": "Sudden drop", "Where": f"{name} ({roll})",
                                "Detail": f"{op:.0f}% earlier -> {rp:.0f}% in the last 2 weeks"})

        # 3. a subject everyone skips compared to the rest
        s = self.subject_table()
        if len(s) > 2 and s.iloc[0]["Percent"] < s["Percent"].mean() - 8:
            out.append({"Type": "Weak subject", "Where": s.iloc[0]["Subject"],
                        "Detail": f"{s.iloc[0]['Percent']}% vs class average "
                                  f"{s['Percent'].mean():.1f}%"})

        MEM.log(self.name, "detect_anomalies", "", f"{len(out)} flagged")
        return pd.DataFrame(out) if out else pd.DataFrame(
            columns=["Type", "Where", "Detail"])

    def stats(self):
        st, sb = self.student_table(), self.subject_table()
        d = ST.df
        return {
            "records": int(len(d)),
            "students": int(st.shape[0]),
            "subjects": int(sb.shape[0]),
            "days": int(d["Date"].dt.date.nunique()),
            "overall": round(float(d["Present"].mean() * 100), 2),
            "threshold": ST.threshold,
            "defaulters": int((st["Percent"] < ST.threshold).sum()),
            "best_subject": sb.iloc[-1]["Subject"],
            "best_pct": float(sb.iloc[-1]["Percent"]),
            "worst_subject": sb.iloc[0]["Subject"],
            "worst_pct": float(sb.iloc[0]["Percent"]),
            "lowest": f"{st.iloc[0]['Name']} ({st.iloc[0]['RollNo']})",
            "lowest_pct": float(st.iloc[0]["Percent"]),
            "from": str(d["Date"].min().date()),
            "to": str(d["Date"].max().date()),
        }


# ======================================================================
#  Agent 3 - Visualisation agent
# ======================================================================
class VisualAgent:
    name = "VisualAgent"

    def all_charts(self, an):
        paths = []
        th = ST.threshold

        sb = an.subject_table()
        fig, ax = plt.subplots(figsize=(7, 3.6))
        ax.barh(sb["Subject"], sb["Percent"],
                color=[RED if p < th else BLUE for p in sb["Percent"]])
        ax.axvline(th, color=GREY, ls="--", lw=1)
        for y, p in enumerate(sb["Percent"]):
            ax.text(p + 1, y, f"{p}%", va="center", fontsize=8, color=MUTED)
        ax.set_xlim(0, 105)
        ax.set_xlabel("attendance %")
        ax.set_title("Subject wise attendance", loc="left", fontsize=11,
                     weight="bold", color=TEXT)
        paths.append(self._save(fig, "chart_subject.png"))

        st = an.student_table()
        fig, ax = plt.subplots(figsize=(7, 3.4))
        ax.bar(range(len(st)), st["Percent"],
               color=[RED if p < th else (AMBER if p < th + 8 else GREEN)
                      for p in st["Percent"]])
        ax.axhline(th, color=GREY, ls="--", lw=1)
        ax.set_xticks(range(len(st)))
        ax.set_xticklabels(st["RollNo"], rotation=75, fontsize=7)
        ax.set_ylim(0, 100)
        ax.set_ylabel("attendance %")
        ax.set_title("Every student against the cut-off", loc="left",
                     fontsize=11, weight="bold", color=TEXT)
        paths.append(self._save(fig, "chart_students.png"))

        d = an.daily()
        roll = d["Percent"].rolling(5, min_periods=1).mean()
        fig, ax = plt.subplots(figsize=(7, 3.2))
        ax.plot(d["Date"], d["Percent"], color=GREY, lw=0.9, marker="o", ms=2.5,
                label="daily")
        ax.plot(d["Date"], roll, color=BLUE, lw=2, label="5 day average")
        ax.axhline(th, color=RED, ls="--", lw=1, label=f"cut-off {th}%")
        ax.set_ylim(0, 100)
        ax.legend(frameon=False, fontsize=8, labelcolor=MUTED)
        ax.set_title("Day wise attendance trend", loc="left", fontsize=11,
                     weight="bold", color=TEXT)
        plt.xticks(rotation=30, ha="right", fontsize=7)
        paths.append(self._save(fig, "chart_trend.png"))

        p = an.pivot()
        fig, ax = plt.subplots(figsize=(7, 0.32 * len(p) + 1.8))
        im = ax.imshow(p.values, cmap="RdYlGn", vmin=40, vmax=100, aspect="auto")
        ax.set_xticks(range(len(p.columns)))
        ax.set_xticklabels(p.columns, rotation=20, ha="right", fontsize=8)
        ax.set_yticks(range(len(p.index)))
        ax.set_yticklabels(p.index, fontsize=7)
        ax.grid(False)
        for i in range(len(p.index)):
            for j in range(len(p.columns)):
                ax.text(j, i, f"{p.values[i, j]:.0f}", ha="center", va="center",
                        fontsize=6.5, color="#0b0f17")
        fig.colorbar(im, ax=ax, shrink=0.7, label="%")
        ax.set_title("Student x subject heatmap", loc="left", fontsize=11,
                     weight="bold", color=TEXT)
        paths.append(self._save(fig, "chart_heatmap.png"))

        fc = an.forecast()
        fig, ax = plt.subplots(figsize=(6.4, 4.2))
        cmap = {"High": RED, "Watch": AMBER, "Medium": AMBER, "Low": GREEN}
        ax.scatter(fc["Current"], fc["Projected"],
                   c=[cmap[r] for r in fc["Risk"]], s=55, edgecolor=INK,
                   linewidth=0.8, zorder=3)
        lims = [min(fc["Current"].min(), fc["Projected"].min()) - 5,
                max(fc["Current"].max(), fc["Projected"].max()) + 5]
        ax.plot(lims, lims, color=GREY, lw=0.8, ls=":")
        ax.axhline(th, color=RED, ls="--", lw=1)
        ax.axvline(th, color=RED, ls="--", lw=1)
        for _, r in fc[fc["Risk"].isin(["High", "Watch"])].iterrows():
            ax.annotate(r["RollNo"], (r["Current"], r["Projected"]),
                        fontsize=6.5, xytext=(3, 3), textcoords="offset points")
        ax.set_xlabel("attendance today %")
        ax.set_ylabel(f"projected after {CLASSES_LEFT} more classes %")
        ax.set_title("Where each student is heading", loc="left",
                     fontsize=11, weight="bold", color=TEXT)
        paths.append(self._save(fig, "chart_forecast.png"))

        ST.charts = paths
        MEM.log(self.name, "render_charts", "", f"{len(paths)} charts")
        return paths

    @staticmethod
    def _save(fig, name):
        path = os.path.join(OUT_DIR, name)
        fig.tight_layout()
        fig.savefig(path, dpi=130)
        plt.close(fig)
        return path


# ======================================================================
#  Agent 4 - Communication agent : drafts the warning mails
# ======================================================================
class CommsAgent:
    name = "CommsAgent"

    def __init__(self, llm):
        self.llm = llm

    def draft(self, an, limit=10):
        st = an.student_table()
        fc = an.forecast().set_index("RollNo")
        targets = st[st["Percent"] < ST.threshold].head(limit)

        for f in os.listdir(MAIL_DIR):
            os.remove(os.path.join(MAIL_DIR, f))

        made = []
        for _, r in targets.iterrows():
            need = int(fc.loc[r["RollNo"], "NeedToAttend"]) if r["RollNo"] in fc.index else 0
            body = self._body(r, need)
            path = os.path.join(MAIL_DIR, f"{r['RollNo']}_warning.txt")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body)
            made.append({"RollNo": r["RollNo"], "Name": r["Name"],
                         "Percent": r["Percent"], "MustAttend": need,
                         "File": os.path.basename(path)})

        zip_path = os.path.join(OUT_DIR, "warning_emails.zip")
        with zipfile.ZipFile(zip_path, "w") as z:
            for m in made:
                z.write(os.path.join(MAIL_DIR, m["File"]), m["File"])

        MEM.log(self.name, "draft_emails", f"below {ST.threshold}%",
                f"{len(made)} letters written to /{MAIL_DIR}")
        return pd.DataFrame(made), zip_path

    def _body(self, r, need):
        best = AN.best_possible(r["Attended"], r["Classes"])
        if need > CLASSES_LEFT:
            action = (f"Only {CLASSES_LEFT} classes remain this semester, so even "
                      f"with full attendance the student can reach at most {best}%. "
                      f"Please meet the class teacher regarding extra classes or "
                      f"condonation.")
        else:
            action = (f"The student must attend the next {need} of the "
                      f"{CLASSES_LEFT} remaining classes without any absence to "
                      f"become eligible.")
        facts = (f"Student {r['Name']} (roll {r['RollNo']}) has {r['Percent']}% "
                 f"attendance against the required {ST.threshold}%. "
                 f"They attended {r['Attended']} of {r['Classes']} classes. {action}")
        if self.llm.enabled:
            out = self.llm.plain(
                "Write a short, polite, formal warning email from the class teacher "
                "to a student's parent. Max 130 words. Use only these facts, do not "
                "invent anything, no placeholders like [name]:\n" + facts)
            if out:
                return out
        return (f"Subject: Attendance shortage - {r['Name']} ({r['RollNo']})\n\n"
                f"Dear Parent / Guardian,\n\n"
                f"This is to inform you that {r['Name']} (roll number {r['RollNo']}) "
                f"currently has {r['Percent']}% attendance, which is below the "
                f"{ST.threshold}% required by the institute. Out of {r['Classes']} "
                f"classes conducted, {r['Attended']} were attended.\n\n"
                f"{action} Kindly ensure regular attendance from the coming week.\n\n"
                f"Regards,\nClass Teacher\nDepartment of Computer Science\n")


# ======================================================================
#  Agent 5 - Report agent
# ======================================================================
class ReportAgent:
    name = "ReportAgent"

    def build(self, an, summary, charts):
        s = an.stats()
        files = []

        xlsx = os.path.join(OUT_DIR, "attendance_report.xlsx")
        try:
            with pd.ExcelWriter(xlsx) as w:
                pd.DataFrame(s.items(), columns=["Metric", "Value"]).to_excel(
                    w, sheet_name="Summary", index=False)
                an.student_table().to_excel(w, sheet_name="Student wise", index=False)
                an.subject_table().to_excel(w, sheet_name="Subject wise", index=False)
                an.forecast().to_excel(w, sheet_name="Risk forecast", index=False)
                an.anomalies().to_excel(w, sheet_name="Anomalies", index=False)
            files.append(xlsx)
        except Exception as e:
            MEM.log(self.name, "excel_failed", str(e)[:80], "writing csv instead")
            csv = os.path.join(OUT_DIR, "attendance_report.csv")
            an.student_table().to_csv(csv, index=False)
            files.append(csv)

        pdf = os.path.join(OUT_DIR, "attendance_report.pdf")
        with PdfPages(pdf) as book:
            self._cover(book, s, summary, an)
            self._charts_page(book, charts[:3], "Charts")
            self._charts_page(book, charts[3:], "Risk analysis")
            self._risk_page(book, an)
        files.append(pdf)

        ST.reports = files
        MEM.log(self.name, "write_report", "", ", ".join(os.path.basename(f) for f in files))
        return files

    def _cover(self, book, s, summary, an):
        fig = plt.figure(figsize=(8.27, 11.69), facecolor=INK)
        fig.text(0.07, 0.945, "Attendance Report", fontsize=23, weight="bold",
                 color=TEXT)
        fig.text(0.07, 0.921, f"{s['from']} to {s['to']}   |   source: {ST.source}",
                 fontsize=9.5, color=MUTED)
        fig.patches.append(plt.Rectangle((0.07, 0.912), 0.86, 0.0035,
                                         transform=fig.transFigure, color=CYAN))

        cards = [("Overall", f"{s['overall']}%"), ("Cut-off", f"{s['threshold']}%"),
                 ("Students", s["students"]), ("Defaulters", s["defaulters"])]
        for i, (k, v) in enumerate(cards):
            x = 0.07 + i * 0.218
            fig.patches.append(plt.Rectangle((x, 0.815), 0.20, 0.072,
                                             transform=fig.transFigure,
                                             facecolor=PANEL, edgecolor=LINE))
            fig.text(x + 0.012, 0.862, k, fontsize=8, color=MUTED)
            fig.text(x + 0.012, 0.830, str(v), fontsize=17, weight="bold",
                     color=CYAN)

        block = (f"Records analysed  : {s['records']}\n"
                 f"Subjects          : {s['subjects']}       Class days : {s['days']}\n"
                 f"Best subject      : {s['best_subject']} ({s['best_pct']}%)\n"
                 f"Weakest subject   : {s['worst_subject']} ({s['worst_pct']}%)\n"
                 f"Lowest student    : {s['lowest']} at {s['lowest_pct']}%")
        fig.text(0.07, 0.775, block, fontsize=10, family="monospace", va="top",
                 color=TEXT)

        fig.text(0.07, 0.675, "Observations", fontsize=13, weight="bold", color=CYAN)
        fig.text(0.07, 0.652, self._wrap(summary, 90), fontsize=9.5, va="top")

        fc = an.forecast()
        high = fc[fc["Risk"].isin(["High", "Watch"])].head(14)
        fig.text(0.07, 0.40, "Students needing action", fontsize=13, weight="bold",
                 color=CYAN)
        head = f"{'Roll':<11}{'Name':<22}{'Now':>7}{'Proj':>8}{'Attend':>8}  Risk"
        rows = [head, "-" * 66]
        for _, r in high.iterrows():
            rows.append(f"{r['RollNo']:<11}{str(r['Name'])[:20]:<22}"
                        f"{r['Current']:>6.1f}%{r['Projected']:>7.1f}%"
                        f"{r['NeedToAttend']:>7}  {r['Risk']}")
        if len(rows) == 2:
            rows.append("No student is at risk.")
        fig.text(0.07, 0.377, "\n".join(rows), fontsize=8.2,
                 family="monospace", va="top")

        fig.text(0.07, 0.04, "Generated by the Attendance Intelligence System "
                             f"on {dt.datetime.now():%d %b %Y, %H:%M}",
                 fontsize=8, color="#888")
        book.savefig(fig, facecolor=INK)
        plt.close(fig)

    def _charts_page(self, book, charts, title):
        if not charts:
            return
        fig = plt.figure(figsize=(8.27, 11.69), facecolor=INK)
        fig.text(0.07, 0.95, title, fontsize=15, weight="bold", color=CYAN)
        for i, cp in enumerate(charts):
            ax = fig.add_axes([0.07, 0.64 - i * 0.30, 0.86, 0.27])
            ax.imshow(plt.imread(cp))
            ax.axis("off")
            ax.grid(False)
        book.savefig(fig, facecolor=INK)
        plt.close(fig)

    def _risk_page(self, book, an):
        fig = plt.figure(figsize=(8.27, 11.69), facecolor=INK)
        fig.text(0.07, 0.95, "Anomalies detected", fontsize=15, weight="bold",
                 color=CYAN)
        a = an.anomalies()
        if len(a):
            txt = "\n".join(f"- {r['Type']}: {r['Where']} - {r['Detail']}"
                            for _, r in a.iterrows())
        else:
            txt = "Nothing unusual found in this data."
        fig.text(0.07, 0.92, self._wrap(txt, 90), fontsize=9, va="top")

        fig.text(0.07, 0.72, "Full student list", fontsize=15, weight="bold", color=CYAN)
        st = an.student_table()
        rows = [f"{'Roll':<11}{'Name':<22}{'Classes':>8}{'Present':>9}{'%':>8}  Remark",
                "-" * 70]
        for _, r in st.iterrows():
            rows.append(f"{r['RollNo']:<11}{str(r['Name'])[:20]:<22}{r['Classes']:>8}"
                        f"{r['Attended']:>9}{r['Percent']:>7.1f}%  {r['Remark']}")
        fig.text(0.07, 0.695, "\n".join(rows), fontsize=8, family="monospace", va="top")
        book.savefig(fig, facecolor=INK)
        plt.close(fig)

    @staticmethod
    def _wrap(text, width):
        out = []
        for line in text.split("\n"):
            while len(line) > width:
                cut = line.rfind(" ", 0, width)
                cut = cut if cut > 0 else width
                out.append(line[:cut])
                line = "   " + line[cut:].strip()
            out.append(line)
        return "\n".join(out)


# ======================================================================
#  LLM wrapper - Groq. Everything degrades to offline mode if no key.
# ======================================================================
class LLM:
    def __init__(self):
        self.key = ""

    @property
    def enabled(self):
        return bool(self.key and self.key.strip())

    def _post(self, payload):
        r = requests.post(GROQ_URL,
                          headers={"Authorization": f"Bearer {self.key.strip()}",
                                   "Content-Type": "application/json"},
                          json=payload, timeout=60)
        r.raise_for_status()
        return r.json()

    def plain(self, prompt):
        try:
            out = self._post({"model": GROQ_MODEL, "temperature": 0.3,
                              "max_tokens": 600,
                              "messages": [{"role": "user", "content": prompt}]})
            return out["choices"][0]["message"]["content"].strip()
        except Exception as e:
            MEM.log("LLM", "groq_error", str(e)[:90], "falling back to offline")
            return None

    def with_tools(self, messages, tools):
        return self._post({"model": GROQ_MODEL, "temperature": 0.2,
                           "max_tokens": 900, "messages": messages,
                           "tools": tools, "tool_choice": "auto"})


LLM_ = LLM()


# ======================================================================
#  Tool layer - the functions the supervisor is allowed to call
# ======================================================================
AN = AnalyticsAgent()
DATA = DataAgent()
VIS = VisualAgent()
COMMS = CommsAgent(LLM_)
REPORT = ReportAgent()


def _find_student(query):
    st = AN.student_table()
    q = str(query).strip().lower()
    hit = st[st["RollNo"].str.lower() == q]
    if not len(hit):
        hit = st[st["Name"].str.lower().str.contains(q, na=False)]
    if not len(hit):
        hit = st[st["RollNo"].str.lower().str.contains(q, na=False)]
    return hit


def t_class_summary():
    s = AN.stats()
    return (f"Between {s['from']} and {s['to']} the class attended "
            f"{s['overall']}% of {s['records']} class records. "
            f"{s['defaulters']} of {s['students']} students are below "
            f"{s['threshold']}%. Best subject {s['best_subject']} ({s['best_pct']}%), "
            f"weakest {s['worst_subject']} ({s['worst_pct']}%).")


def t_student_report(student):
    hit = _find_student(student)
    if not len(hit):
        return f"No student matched '{student}'."
    r = hit.iloc[0]
    fc = AN.forecast()
    f = fc[fc["RollNo"] == r["RollNo"]].iloc[0]
    sub = (ST.df[ST.df["RollNo"] == r["RollNo"]]
           .groupby("Subject")["Present"].mean() * 100).round(1)
    subtxt = ", ".join(f"{k} {v}%" for k, v in sub.items())
    line = (f"{r['Name']} ({r['RollNo']}): {r['Percent']}% - attended "
            f"{r['Attended']} of {r['Classes']} classes, status {r['Remark']}. "
            f"Subject wise: {subtxt}. Weekly trend {f['Trend']:+.2f} points, "
            f"projected {f['Projected']}% by semester end, risk {f['Risk']}.")
    if r["Percent"] < ST.threshold:
        line += f" Must attend the next {f['NeedToAttend']} classes to reach {ST.threshold}%."
    else:
        line += f" Can afford to miss {f['CanMiss']} more classes."
    return line


def t_list_defaulters(subject=None):
    st = AN.student_table(subject)
    d = st[st["Percent"] < ST.threshold]
    where = f" in {subject}" if subject else ""
    if not len(d):
        return f"No student is below {ST.threshold}%{where}."
    lst = "; ".join(f"{r['Name']} ({r['RollNo']}) {r['Percent']}%"
                    for _, r in d.iterrows())
    return f"{len(d)} defaulters{where} below {ST.threshold}%: {lst}"


def t_subject_report(subject=None):
    sb = AN.subject_table()
    if subject:
        hit = sb[sb["Subject"].str.lower().str.contains(subject.lower())]
        if not len(hit):
            return f"No subject matched '{subject}'. Available: " + \
                   ", ".join(sb["Subject"])
        r = hit.iloc[0]
        return (f"{r['Subject']}: {r['Percent']}% attendance over {r['Classes']} "
                f"records ({r['Attended']} present).")
    return "Subject wise attendance - " + "; ".join(
        f"{r['Subject']} {r['Percent']}%" for _, r in sb.iterrows())


def t_classes_needed(student, target=None):
    target = float(target or ST.threshold)
    hit = _find_student(student)
    if not len(hit):
        return f"No student matched '{student}'."
    r = hit.iloc[0]
    n = AN.classes_needed(r["Attended"], r["Classes"], target)
    if n == 0:
        miss = AN.safe_bunks(r["Attended"], r["Classes"], target)
        return (f"{r['Name']} is already at {r['Percent']}%, above {target}%. "
                f"Can still miss {miss} classes and stay above it.")
    best = AN.best_possible(r["Attended"], r["Classes"])
    if n > CLASSES_LEFT:
        return (f"{r['Name']} is at {r['Percent']}% and would need {n} more classes "
                f"to reach {target}%, but only {CLASSES_LEFT} are left this semester. "
                f"Even with perfect attendance from now the maximum reachable is "
                f"{best}%, so this is a condonation / extra class case.")
    return (f"{r['Name']} ({r['Percent']}%) must attend the next {n} of the "
            f"{CLASSES_LEFT} remaining classes without a break to reach {target}%.")


def t_risk_forecast():
    fc = AN.forecast()
    high = fc[fc["Risk"] == "High"]
    watch = fc[fc["Risk"] == "Watch"]
    parts = [f"{len(high)} students are projected to stay below {ST.threshold}% "
             f"even after {CLASSES_LEFT} more classes"]
    if len(high):
        parts.append("high risk: " + ", ".join(
            f"{r['Name']} ({r['Current']}% now, {r['Projected']}% projected)"
            for _, r in high.head(6).iterrows()))
    if len(watch):
        parts.append("above the cut-off today but falling: " + ", ".join(
            f"{r['Name']} ({r['Trend']:+.1f}/week)" for _, r in watch.iterrows()))
    return ". ".join(parts) + "."


def t_detect_anomalies():
    a = AN.anomalies()
    if not len(a):
        return "No anomalies found."
    return "Found " + str(len(a)) + " anomalies: " + "; ".join(
        f"{r['Type']} - {r['Where']} ({r['Detail']})" for _, r in a.iterrows())


def t_draft_warning_emails():
    made, _ = COMMS.draft(AN)
    if not len(made):
        return "Nobody is below the cut-off, no letters needed."
    return (f"Drafted {len(made)} warning letters (saved in the Actions tab): " +
            ", ".join(f"{r['Name']} ({r['RollNo']})" for _, r in made.iterrows()))


def t_generate_report():
    charts = VIS.all_charts(AN)
    files = REPORT.build(AN, t_class_summary(), charts)
    return "Report written: " + ", ".join(os.path.basename(f) for f in files)


def t_set_threshold(value):
    old = ST.threshold
    ST.threshold = int(float(value))
    MEM.log("Supervisor", "set_threshold", f"{old} -> {ST.threshold}")
    return f"Cut-off changed from {old}% to {ST.threshold}%."


def t_compare_subjects():
    sb = AN.subject_table()
    gap = sb.iloc[-1]["Percent"] - sb.iloc[0]["Percent"]
    return (f"{sb.iloc[-1]['Subject']} leads at {sb.iloc[-1]['Percent']}% and "
            f"{sb.iloc[0]['Subject']} trails at {sb.iloc[0]['Percent']}%, a gap of "
            f"{gap:.1f} points. Full list - " + "; ".join(
                f"{r['Subject']} {r['Percent']}%" for _, r in sb.iterrows()))


TOOLS = {
    "class_summary": t_class_summary,
    "student_report": t_student_report,
    "list_defaulters": t_list_defaulters,
    "subject_report": t_subject_report,
    "classes_needed": t_classes_needed,
    "risk_forecast": t_risk_forecast,
    "detect_anomalies": t_detect_anomalies,
    "draft_warning_emails": t_draft_warning_emails,
    "generate_report": t_generate_report,
    "set_threshold": t_set_threshold,
    "compare_subjects": t_compare_subjects,
}

# same tools described in OpenAI / Groq function calling format
TOOL_SPECS = [
    {"type": "function", "function": {
        "name": "class_summary", "description":
        "Overall attendance of the whole class with best and weakest subject.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "student_report", "description":
        "Full attendance card of one student: percentage, subject split, trend, "
        "projection and how many classes are still needed.",
        "parameters": {"type": "object", "properties": {
            "student": {"type": "string", "description": "roll number or name"}},
            "required": ["student"]}}},
    {"type": "function", "function": {
        "name": "list_defaulters", "description":
        "Students below the attendance cut-off, optionally inside one subject.",
        "parameters": {"type": "object", "properties": {
            "subject": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "subject_report", "description":
        "Attendance of one subject, or of every subject if none is given.",
        "parameters": {"type": "object", "properties": {
            "subject": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "classes_needed", "description":
        "How many classes in a row a student must attend to reach a target "
        "percentage, or how many they can still miss.",
        "parameters": {"type": "object", "properties": {
            "student": {"type": "string"},
            "target": {"type": "number", "description": "target percent"}},
            "required": ["student"]}}},
    {"type": "function", "function": {
        "name": "risk_forecast", "description":
        "Predicts who will finish the semester short of the requirement, "
        "including students who are safe now but falling.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "detect_anomalies", "description":
        "Finds mass absence days, sudden drops and unusually weak subjects.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "draft_warning_emails", "description":
        "Writes warning letters for every defaulter and saves them as files.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "generate_report", "description":
        "Builds the PDF and Excel attendance report with all charts.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "set_threshold", "description":
        "Changes the minimum attendance requirement.",
        "parameters": {"type": "object", "properties": {
            "value": {"type": "number"}}, "required": ["value"]}}},
    {"type": "function", "function": {
        "name": "compare_subjects", "description":
        "Compares all subjects and reports the gap between best and worst.",
        "parameters": {"type": "object", "properties": {}}}},
]

SYSTEM_PROMPT = (
    "You are the attendance coordinator's assistant. You can only answer using "
    "the tools provided - never guess a number. Call as many tools as you need, "
    "then reply in 2-4 short sentences in plain English. Quote the exact figures "
    "the tools return."
)


# ======================================================================
#  Supervisor agent - picks the tools. LLM if a key is set, else keywords.
# ======================================================================
class Supervisor:
    name = "Supervisor"
    MAX_STEPS = 4

    def ask(self, question):
        MEM.new_trace()
        MEM.log(self.name, "receive", question[:120])
        if not ST.loaded:
            return "Load an attendance file first (Dashboard tab).", MEM.trace_text()

        if LLM_.enabled:
            try:
                return self._llm_loop(question)
            except Exception as e:
                MEM.log(self.name, "llm_failed", str(e)[:90], "switching to offline planner")
        return self._offline(question)

    # ---- LLM driven: real function calling loop ---------------------
    def _llm_loop(self, question):
        messages = [{"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": question}]
        MEM.log(self.name, "plan", f"groq {GROQ_MODEL}", "deciding which tools to call")

        for step in range(self.MAX_STEPS):
            resp = LLM_.with_tools(messages, TOOL_SPECS)
            msg = resp["choices"][0]["message"]
            calls = msg.get("tool_calls") or []
            messages.append(msg)

            if not calls:
                answer = (msg.get("content") or "").strip()
                MEM.log(self.name, "answer", f"step {step + 1}", "no more tools needed")
                return answer, MEM.trace_text()

            for c in calls:
                fn = c["function"]["name"]
                try:
                    args = json.loads(c["function"]["arguments"] or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = self._run(fn, args)
                messages.append({"role": "tool", "tool_call_id": c["id"],
                                 "name": fn, "content": result})

        MEM.log(self.name, "stop", "step limit reached")
        return "I could not finish that in the allowed number of steps.", MEM.trace_text()

    def _run(self, fn, args):
        if fn not in TOOLS:
            return f"unknown tool {fn}"
        MEM.log("ToolCall", fn, json.dumps(args) if args else "")
        try:
            out = str(TOOLS[fn](**args))
        except TypeError:
            out = str(TOOLS[fn]())
        except Exception as e:
            out = f"tool failed: {e}"
        MEM.log("ToolResult", fn, "", out[:220])
        return out

    # ---- offline planner: keyword rules over the same tools ---------
    def _offline(self, q):
        text = q.lower().strip()
        MEM.log(self.name, "plan", "offline keyword planner")
        plan = []

        m = re.search(r"(?:cut ?off|threshold|requirement)\D{0,15}(\d{2})", text)
        if m and any(w in text for w in ("set", "change", "make", "to")):
            plan.append(("set_threshold", {"value": float(m.group(1))}))

        subject = None
        if ST.loaded:
            for s in ST.df["Subject"].unique():
                if s.lower() in text:
                    subject = s
                    break

        person = None
        if ST.loaded:
            roll = re.search(r"\b\d{2}[a-z]{2}\d{3}\b", text)
            if roll:
                person = roll.group(0)
            else:
                for n in ST.df["Name"].unique():
                    first = n.split()[0].lower()
                    if len(first) > 3 and first in text:
                        person = n
                        break

        if any(w in text for w in ("mail", "email", "letter", "notice", "warn parent")):
            plan.append(("draft_warning_emails", {}))
        if any(w in text for w in ("report", "pdf", "excel", "download")):
            plan.append(("generate_report", {}))
        if any(w in text for w in ("anomal", "unusual", "strange", "mass", "bunk", "drop")):
            plan.append(("detect_anomalies", {}))
        if any(w in text for w in ("predict", "forecast", "risk", "will", "future",
                                   "projected", "end of sem")):
            plan.append(("risk_forecast", {}))
        if any(w in text for w in ("how many", "need", "attend", "reach", "eligible",
                                   "bunk", "miss")) and person:
            tgt = re.search(r"(\d{2})\s*%", text)
            plan.append(("classes_needed",
                         {"student": person,
                          "target": float(tgt.group(1)) if tgt else ST.threshold}))
        if any(w in text for w in ("defaulter", "below", "short", "less than",
                                   "shortage", "fail")):
            plan.append(("list_defaulters", {"subject": subject} if subject else {}))
        if person and not any(p[0] in ("classes_needed",) for p in plan):
            plan.append(("student_report", {"student": person}))
        if subject and not any(p[0] == "list_defaulters" for p in plan):
            plan.append(("subject_report", {"subject": subject}))
        if any(w in text for w in ("compare", "which subject", "best subject",
                                   "worst subject")):
            plan.append(("compare_subjects", {}))
        if not plan:
            plan.append(("class_summary", {}))

        seen, final = set(), []
        for fn, args in plan:
            if fn not in seen:
                seen.add(fn)
                final.append((fn, args))

        MEM.log(self.name, "selected_tools", ", ".join(f for f, _ in final))
        answers = [self._run(fn, args) for fn, args in final]
        MEM.log(self.name, "answer", f"{len(final)} tool(s) used")
        return "\n\n".join(answers), MEM.trace_text()


SUP = Supervisor()


# ======================================================================
#  Sample data generator
# ======================================================================
def make_sample(path=os.path.join(OUT_DIR, "sample_attendance.csv")):
    random.seed(7)
    np.random.seed(7)
    names = ["Aarav Sharma", "Ishika Dubey", "Rohan Patil", "Sneha Iyer",
             "Kabir Mehta", "Ananya Rao", "Vivek Kulkarni", "Priya Nair",
             "Arjun Deshmukh", "Tanvi Joshi", "Yash Agarwal", "Meera Pillai",
             "Nikhil Verma", "Riya Bansal", "Omkar Jadhav"]
    subjects = ["Agentic AI", "Machine Learning", "DBMS", "Computer Networks",
                "Soft Skills"]

    habit = {n: random.uniform(0.80, 0.99) for n in names}
    for n in random.sample(names, 3):                 # chronic defaulters
        habit[n] = random.uniform(0.52, 0.70)
    sliding = random.sample([n for n in names if habit[n] > 0.8], 2)   # start well, fall off

    rows = []
    day, end = dt.date(2026, 7, 1), dt.date(2026, 8, 30)
    total_days = (end - day).days
    while day <= end:
        if day.weekday() < 5:
            progress = (day - dt.date(2026, 7, 1)).days / total_days
            mass_bunk = random.random() < 0.04        # the odd day everyone skips
            for sub in subjects:
                if random.random() < 0.15:
                    continue
                for i, n in enumerate(names):
                    p = habit[n]
                    if sub == "Soft Skills":
                        p *= 0.88
                    if n in sliding:
                        p *= (1 - 0.30 * progress)     # gradual decline
                    if mass_bunk:
                        p *= 0.35
                    rows.append([day.isoformat(), f"23CS{101 + i}", n, sub,
                                 "Present" if random.random() < p else "Absent"])
        day += dt.timedelta(days=1)

    pd.DataFrame(rows, columns=REQUIRED_COLS).to_csv(path, index=False)
    return path


# ======================================================================
#  Gradio UI
# ======================================================================
def make(component, **kw):
    """
    Gradio keeps renaming arguments between versions (type=, object_fit=,
    show_copy_button= ...). This drops whatever the installed version does not
    accept instead of crashing on someone else's machine.
    """
    while True:
        try:
            return component(**kw)
        except TypeError as e:
            bad = re.search(r"unexpected keyword argument '(\w+)'", str(e))
            if not bad or bad.group(1) not in kw:
                raise
            kw.pop(bad.group(1))


CSS = """
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap');

:root, .dark {
  --ink:#0b0f17; --panel:#121a27; --panel2:#0e1521; --line:#223049;
  --txt:#e6edf7; --muted:#8fa3bf; --cyan:#22d3ee; --violet:#a78bfa;
  --green:#34d399; --amber:#fbbf24; --red:#f87171;
}

.gradio-container, body, gradio-app {
  background:var(--ink) !important;
  color:var(--txt) !important;
  font-family:'Inter',system-ui,sans-serif !important;
  max-width:100% !important;
}
.gradio-container { padding:0 26px 40px 26px !important; }
footer, .built-with, .show-api { display:none !important; }

/* ---------- hero ---------- */
#hero {
  margin:18px 0 20px 0; padding:26px 30px; border-radius:16px;
  background:
    radial-gradient(900px 260px at 8% -30%, rgba(34,211,238,.22), transparent 60%),
    radial-gradient(700px 240px at 92% -40%, rgba(167,139,250,.20), transparent 60%),
    linear-gradient(180deg,#131c2b,#0d131e);
  border:1px solid var(--line);
}
#hero h1 {
  margin:0; font-size:31px; font-weight:700; letter-spacing:-.6px; color:#fff;
}
#hero .sub { color:var(--muted); font-size:13.5px; margin-top:6px; }
#hero .chips { margin-top:16px; display:flex; gap:8px; flex-wrap:wrap; }
#hero .chip {
  font-family:'JetBrains Mono',monospace; font-size:11px; padding:5px 11px;
  border-radius:99px; border:1px solid rgba(34,211,238,.35);
  background:rgba(34,211,238,.09); color:#9beaf7;
}
#hero .chip.v { border-color:rgba(167,139,250,.35); background:rgba(167,139,250,.09);
  color:#cdbdfd; }

/* ---------- kpi row ---------- */
.kpi { display:grid; gap:11px; grid-template-columns:repeat(6,1fr); }
@media (max-width:1180px){ .kpi { grid-template-columns:repeat(3,1fr); } }
@media (max-width:640px) { .kpi { grid-template-columns:repeat(2,1fr); } }

/* ---------- panels ---------- */
.xcard {
  background:var(--panel) !important; border:1px solid var(--line) !important;
  border-radius:14px !important; padding:16px !important;
}
.xcard .block, .xcard .form { background:transparent !important; border:none !important; }

.sec-label {
  font-family:'JetBrains Mono',monospace; font-size:10.5px; letter-spacing:1.6px;
  text-transform:uppercase; color:var(--muted); margin:0 0 10px 2px;
}

/* form controls */
input[type=text], input[type=password], input[type=number], textarea, select {
  background:var(--panel2) !important; color:var(--txt) !important;
  border:1px solid var(--line) !important; border-radius:9px !important;
}
label span, .gr-check-radio label, span[data-testid] { color:var(--muted) !important; }
button.primary, .primary button, button[variant=primary] {
  background:linear-gradient(90deg,#22d3ee,#6366f1) !important; color:#06131a !important;
  border:none !important; font-weight:700 !important; border-radius:10px !important;
}
button.secondary, .secondary button {
  background:var(--panel2) !important; color:var(--txt) !important;
  border:1px solid var(--line) !important; border-radius:10px !important;
}
input[type=range]::-webkit-slider-thumb { background:var(--cyan) !important; }

/* file drop zone */
.file-preview, .upload-container, [data-testid=block-label] { color:var(--muted) !important; }

/* ---------- terminal trace ---------- */
#trace textarea {
  background:#070b12 !important; color:#7ef2c4 !important;
  font-family:'JetBrains Mono',monospace !important; font-size:11.5px !important;
  line-height:1.65 !important; border:1px solid var(--line) !important;
  border-radius:12px !important;
}
#narrative textarea {
  background:var(--panel2) !important; color:var(--txt) !important;
  font-size:13.5px !important; line-height:1.7 !important; border:none !important;
}

/* ---------- tabs ---------- */
.tab-nav, .tabs > .tab-nav { border-bottom:1px solid var(--line) !important; gap:4px !important; }
.tab-nav button, button.tab {
  color:var(--muted) !important; background:transparent !important;
  border:none !important; font-size:13px !important; font-weight:500 !important;
  padding:9px 14px !important; border-radius:9px 9px 0 0 !important;
}
.tab-nav button.selected, button.tab.selected {
  color:var(--cyan) !important; background:rgba(34,211,238,.10) !important;
  border-bottom:2px solid var(--cyan) !important;
}

/* ---------- tables ---------- */
table { background:var(--panel) !important; color:var(--txt) !important;
        font-size:12.5px !important; }
table thead th {
  background:#101a2a !important; color:#9beaf7 !important;
  font-family:'JetBrains Mono',monospace !important; font-size:11px !important;
  letter-spacing:.6px !important; text-transform:uppercase !important;
  border-bottom:1px solid var(--line) !important;
}
table td { border-color:#1a2537 !important; }
table tbody tr:hover td { background:rgba(34,211,238,.06) !important; }

/* ---------- chat ---------- */
.message-row .message, .bubble-wrap, .message {
  background:var(--panel) !important; border:1px solid var(--line) !important;
  color:var(--txt) !important; border-radius:12px !important;
}
.message-row.user-row .message, .user .message {
  background:linear-gradient(90deg,rgba(34,211,238,.16),rgba(99,102,241,.16)) !important;
  border-color:rgba(34,211,238,.35) !important;
}
.chatbot, .chatbot-wrap { background:var(--panel2) !important; border-radius:12px !important; }
code, .md code { background:#0a1119 !important; color:#7ef2c4 !important;
                 font-family:'JetBrains Mono',monospace !important; }

/* gallery */
.gallery, .grid-wrap, .thumbnail-item { background:var(--panel2) !important;
  border-color:var(--line) !important; }

.prose, .md, .markdown, p, li { color:var(--txt) !important; }
.prose em { color:var(--muted) !important; }
"""

FORCE_DARK = """
function(){
  const u = new URL(window.location);
  if (u.searchParams.get('__theme') !== 'dark') {
    u.searchParams.set('__theme','dark');
    window.location.replace(u.href);
  }
}
"""

HERO = """
<div id='hero'>
  <h1>Attendance Intelligence System</h1>
  <div class='sub'>Agentic AI and Automation &nbsp;·&nbsp; Unit Test 3 &nbsp;·&nbsp;
  six agents, eleven callable tools, one supervisor that decides what to run</div>
  <div class='chips'>
    <span class='chip'>DataAgent</span>
    <span class='chip'>AnalyticsAgent</span>
    <span class='chip'>VisualAgent</span>
    <span class='chip'>InsightAgent</span>
    <span class='chip v'>CommsAgent</span>
    <span class='chip v'>ReportAgent</span>
    <span class='chip v'>Supervisor · tool calling</span>
  </div>
</div>
"""

IDLE_KPI = """
<div style='border:1px dashed #223049;border-radius:14px;padding:34px;
            text-align:center;color:#8fa3bf;font-size:13px'>
  Nothing loaded yet — press <b style='color:#22d3ee'>Run the agents</b> to start
  the pipeline.
</div>
"""


def kpi_html(s):
    def card(label, value, accent="#e6edf7", foot=""):
        return f"""
        <div style='background:#121a27;border:1px solid #223049;
                    border-radius:13px;padding:13px 15px;position:relative;
                    overflow:hidden'>
          <div style='position:absolute;left:0;top:0;bottom:0;width:3px;
                      background:{accent}'></div>
          <div style='font-family:JetBrains Mono,monospace;font-size:10px;
                      letter-spacing:1.3px;text-transform:uppercase;color:#8fa3bf'>
            {label}</div>
          <div style='font-size:26px;font-weight:700;color:{accent};
                      margin-top:5px;line-height:1.1'>{value}</div>
          <div style='font-size:11px;color:#8fa3bf;margin-top:3px'>{foot}</div>
        </div>"""

    ok = s["overall"] >= s["threshold"]
    fc = AN.forecast()
    watch = int((fc["Risk"] == "Watch").sum())
    return (
        "<div class='kpi'>"
        + card("Overall", f"{s['overall']}%", GREEN if ok else RED,
               ("above" if ok else "below") + f" the {s['threshold']}% cut-off")
        + card("Defaulters", s["defaulters"], RED if s["defaulters"] else GREEN,
               f"of {s['students']} students")
        + card("Sliding", watch, AMBER if watch else GREEN,
               "safe today, falling fast")
        + card("Class days", s["days"], CYAN, f"{s['records']} records")
        + card("Weakest", f"{s['worst_pct']}%", VIOLET, s["worst_subject"])
        + card("Strongest", f"{s['best_pct']}%", CYAN, s["best_subject"])
        + "</div>"
        + f"<div style='margin-top:10px;font-size:11.5px;color:#8fa3bf;"
          f"font-family:JetBrains Mono,monospace'>window {s['from']} → {s['to']} "
          f"&nbsp;·&nbsp; source {ST.source} &nbsp;·&nbsp; planner "
          f"{'groq ' + GROQ_MODEL if LLM_.enabled else 'offline rule engine'}</div>"
    )


def run_pipeline(file_obj, threshold, use_sample, key):
    LLM_.key = key or ""
    ST.threshold = int(threshold)
    MEM.new_trace()
    MEM.log("Orchestrator", "run_pipeline", f"cut-off {ST.threshold}%")
    t0 = time.time()

    path = make_sample() if (use_sample or file_obj is None) else (
        file_obj.name if hasattr(file_obj, "name") else file_obj)

    try:
        DATA.load(path)
    except Exception as e:
        MEM.log("Orchestrator", "aborted", str(e)[:120])
        bad = (f"<div style='border:1px solid #f87171;background:rgba(248,113,113,.09);"
               f"border-radius:13px;padding:18px;color:#fecaca'>"
               f"<b>The file could not be read</b><br>{e}</div>")
        return (MEM.trace_text(), bad, "", None, None, None, [], None, None)

    summary = _summary_text()
    charts = VIS.all_charts(AN)
    files = REPORT.build(AN, summary, charts)
    MEM.log("Orchestrator", "done", f"{time.time() - t0:.2f}s")

    return (MEM.trace_text(), kpi_html(AN.stats()), summary,
            AN.student_table(), AN.subject_table(), AN.forecast(),
            charts, files, AN.anomalies())


def _summary_text():
    """LLM writes the narrative if a key is present, else the rule engine does."""
    s = AN.stats()
    fc = AN.forecast()
    high = fc[fc["Risk"] == "High"]["Name"].tolist()[:5]
    watch = fc[fc["Risk"] == "Watch"]["Name"].tolist()[:5]

    if LLM_.enabled:
        facts = json.dumps({**s, "high_risk": high, "sliding": watch,
                            "anomalies": AN.anomalies().to_dict("records")[:5]})
        out = LLM_.plain(
            "You are an academic coordinator. Using ONLY the JSON below, write 5 "
            "short bullet points about this class's attendance and end with one "
            "recommended action. Do not invent any number.\n" + facts)
        if out:
            MEM.log("InsightAgent", "summarise", "groq", "LLM narrative")
            return out

    bits = [
        f"- {s['students']} students were tracked over {s['days']} class days "
        f"({s['records']} records) between {s['from']} and {s['to']}.",
        f"- Class attendance stands at {s['overall']}% against the required "
        f"{s['threshold']}%.",
    ]
    if s["defaulters"]:
        bits.append(f"- {s['defaulters']} students are already short, the lowest "
                    f"being {s['lowest']} at {s['lowest_pct']}%.")
    else:
        bits.append("- No student is below the requirement at present.")
    if high:
        bits.append(f"- Projected to stay short even after {CLASSES_LEFT} more "
                    f"classes: {', '.join(high)}.")
    if watch:
        bits.append(f"- Safe today but falling week on week: {', '.join(watch)} - "
                    f"worth an early warning.")
    bits.append(f"- {s['worst_subject']} is the weakest subject at {s['worst_pct']}% "
                f"against {s['best_subject']} at {s['best_pct']}%; review its slot "
                f"in the timetable.")
    MEM.log("InsightAgent", "summarise", "offline", "rule based narrative")
    return "\n".join(bits)


def chat_fn(message, history, key):
    LLM_.key = key or ""
    if not message.strip():
        return history, ""
    answer, trace = SUP.ask(message)
    mode = f"groq · {GROQ_MODEL}" if LLM_.enabled else "offline planner"
    history = (history or []) + [
        {"role": "user", "content": message},
        {"role": "assistant", "content": answer + f"\n\n`planner: {mode}`"},
    ]
    return history, trace


def do_emails(key):
    LLM_.key = key or ""
    if not ST.loaded:
        return pd.DataFrame(), None, "Run the pipeline first."
    made, zpath = COMMS.draft(AN)
    if not len(made):
        return made, None, "Nobody is below the cut-off - no letters were needed."
    preview = open(os.path.join(MAIL_DIR, made.iloc[0]["File"]),
                   encoding="utf-8").read()
    return made, zpath, preview


def refresh_audit():
    return MEM.audit()


# the Inter font itself is pulled in by the @import at the top of CSS
THEME = gr.themes.Base(primary_hue="cyan", neutral_hue="slate")

# Gradio 5 takes the theme/css/js on Blocks, Gradio 6 takes them on launch().
# Passing them in both places keeps the app looking the same on either version.
STYLE = dict(theme=THEME, css=CSS, js=FORCE_DARK)

with make(gr.Blocks, title="Attendance Intelligence System",
          fill_width=True, **STYLE) as demo:

    gr.HTML(HERO)

    with gr.Row(equal_height=False):
        with gr.Column(scale=4, elem_classes="xcard"):
            gr.HTML("<div class='sec-label'>control panel</div>")
            f_in = gr.File(label="Attendance file (CSV / Excel)",
                           file_types=[".csv", ".xlsx", ".xls"], height=110)
            sample_cb = gr.Checkbox(label="Use generated sample data", value=True)
            th_in = gr.Slider(50, 90, value=75, step=1, label="Attendance cut-off %")
            key_in = gr.Textbox(label="Groq API key (optional)", type="password",
                                placeholder="blank = fully offline")
            run_btn = gr.Button("Run the agents", variant="primary", size="lg")
            gr.HTML("<div style='font-size:11px;color:#8fa3bf;line-height:1.6;"
                    "margin-top:10px'>Expected columns "
                    "<code>Date · RollNo · Name · Subject · Status</code><br>"
                    "Without a key every agent falls back to its rule based logic, "
                    "so the demo never breaks.</div>")

        with gr.Column(scale=9):
            kpi_out = gr.HTML(IDLE_KPI)
            with gr.Column(elem_classes="xcard"):
                gr.HTML("<div class='sec-label'>insight agent · narrative</div>")
                insight_out = make(gr.Textbox, show_label=False, lines=7,
                                   elem_id="narrative", container=False,
                                   placeholder="the written summary appears here "
                                               "after the run")

    with gr.Column(elem_classes="xcard"):
        gr.HTML("<div class='sec-label'>agent trace · live</div>")
        trace_out = make(gr.Textbox, show_label=False, lines=13, max_lines=26,
                         elem_id="trace", container=False, show_copy_button=True,
                         placeholder="every agent action, tool call and result "
                                     "is printed here")

    with gr.Tabs():
        with gr.Tab("Ask the agent"):
            gr.HTML("<div style='color:#8fa3bf;font-size:12.5px;margin:10px 2px'>"
                    "The supervisor reads the question, picks tools from its "
                    "registry, runs them and answers only from what they return — "
                    "watch the trace box above fill up with each tool call.</div>")
            chat = make(gr.Chatbot, type="messages", height=330,
                        show_label=False, elem_id="chat", value=[{
                            "role": "assistant",
                            "content": "Supervisor ready. I can call 11 tools over "
                                       "the loaded attendance data - summaries, a "
                                       "single student's card, defaulter lists, "
                                       "how many classes somebody still needs, the "
                                       "end of semester risk forecast, anomaly "
                                       "checks, warning letters and the report "
                                       "builder. Ask in plain English."}])
            with gr.Row():
                msg = gr.Textbox(placeholder="ask anything about the attendance data...",
                                 scale=6, show_label=False, container=False)
                send = gr.Button("Send", variant="primary", scale=1)
            gr.Examples(
                ["Give me the class summary",
                 "Who are the defaulters in Soft Skills?",
                 "How many classes does 23CS111 need to reach 75%?",
                 "Who is at risk of falling short by the end of the semester?",
                 "Is there anything unusual in this data?",
                 "Draft warning mails for the defaulters"],
                inputs=msg, label="")

        with gr.Tab("Students"):
            st_out = make(gr.Dataframe, interactive=False, wrap=True, show_label=False)
        with gr.Tab("Subjects"):
            sb_out = make(gr.Dataframe, interactive=False, show_label=False)
        with gr.Tab("Risk forecast"):
            gr.HTML("<div style='color:#8fa3bf;font-size:12.5px;margin:10px 2px'>"
                    "A line is fitted through each student's weekly attendance and "
                    f"projected over the next {CLASSES_LEFT} classes. "
                    "<b style='color:#fbbf24'>Watch</b> = above the cut-off today "
                    "but sliding. <b style='color:#f87171'>not possible</b> = cannot "
                    "recover even with full attendance.</div>")
            fc_out = make(gr.Dataframe, interactive=False, show_label=False)
        with gr.Tab("Anomalies"):
            an_out = make(gr.Dataframe, interactive=False, wrap=True, show_label=False)
        with gr.Tab("Charts"):
            gal = make(gr.Gallery, columns=2, height=560, object_fit="contain",
                       show_label=False)
        with gr.Tab("Automated actions"):
            gr.HTML("<div style='color:#8fa3bf;font-size:12.5px;margin:10px 2px'>"
                    "The communication agent writes one warning letter per defaulter "
                    "(LLM worded with a key, template otherwise) and zips them.</div>")
            mail_btn = gr.Button("Draft warning letters", variant="primary")
            mail_tbl = make(gr.Dataframe, interactive=False, show_label=False)
            with gr.Row():
                mail_prev = gr.Textbox(label="First letter", lines=14)
                mail_zip = gr.File(label="All letters (zip)")
        with gr.Tab("Downloads"):
            files_out = gr.File(label="Generated report files", file_count="multiple")
        with gr.Tab("Audit log"):
            gr.HTML("<div style='color:#8fa3bf;font-size:12.5px;margin:10px 2px'>"
                    "Every agent action of every run, persisted in "
                    "<code>outputs/agent_memory.db</code>.</div>")
            audit_btn = gr.Button("Refresh", variant="secondary")
            audit_out = make(gr.Dataframe, interactive=False, wrap=True,
                             show_label=False)

    run_btn.click(run_pipeline,
                  inputs=[f_in, th_in, sample_cb, key_in],
                  outputs=[trace_out, kpi_out, insight_out, st_out, sb_out,
                           fc_out, gal, files_out, an_out])
    send.click(chat_fn, [msg, chat, key_in], [chat, trace_out]).then(
        lambda: "", None, msg)
    msg.submit(chat_fn, [msg, chat, key_in], [chat, trace_out]).then(
        lambda: "", None, msg)
    mail_btn.click(do_emails, [key_in], [mail_tbl, mail_zip, mail_prev])
    audit_btn.click(refresh_audit, None, audit_out)


if __name__ == "__main__":
    try:
        demo.launch(**STYLE)
    except TypeError:
        demo.launch()
