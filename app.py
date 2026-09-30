"""
Gradio front-end for the Automated Attendance Report Generator.

Left panel: upload a CSV (or load the bundled sample) and set the
defaulter threshold. Right panel: generate a full Markdown report, or
chat with the agent for follow-up questions -- both are backed by the
same agent pipeline in agent_core.py, and both share persistent memory
keyed by the Session ID box (SQLite-backed, survives app restarts).
"""
import io
import uuid

import gradio as gr
import pandas as pd

import agent_core
from attendance_tools import AttendanceContext

SAMPLE_PATH = "sample_data.csv"


def _preview(csv_text: str) -> pd.DataFrame:
    return pd.read_csv(io.StringIO(csv_text))


def load_sample():
    with open(SAMPLE_PATH, "r", encoding="utf-8") as f:
        text = f.read()
    return text, _preview(text), "Loaded the bundled sample dataset (12 students, 10 sessions)."


def handle_upload(file):
    if file is None:
        return None, None, "⚠️ No file selected. Please upload a CSV file."
    # Gradio ≥4 passes a filepath string; older versions pass an object with .name
    filepath = file if isinstance(file, str) else file.name
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            text = f.read()
    except Exception as exc:
        return None, None, f"❌ Could not read file: {exc}"
    try:
        df = _preview(text)
    except Exception as exc:
        return None, None, f"❌ Could not parse as CSV: {exc}"
    return (
        text,
        df,
        f"✅ Uploaded successfully — **{len(df)} students**, **{len(df.columns) - 1}** session columns.",
    )


async def generate_report(csv_text, threshold, session_id, label):
    if not csv_text:
        return "Please upload an attendance CSV first, or load the sample data.", None
    ctx = AttendanceContext(csv_text=csv_text, threshold=threshold, label=label or "Uploaded Sheet")
    report = await agent_core.run_turn("Generate the attendance report.", ctx, session_id)
    out_path = "generated_report.md"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(report)
    return report, out_path


async def chat_fn(message, history, csv_text, threshold, session_id, label):
    history = history or []
    if not message:
        return history, ""
    if not csv_text:
        history = history + [
            {"role": "user", "content": message},
            {"role": "assistant", "content": "Please upload or load attendance data first."},
        ]
        return history, ""
    ctx = AttendanceContext(csv_text=csv_text, threshold=threshold, label=label or "Uploaded Sheet")
    reply = await agent_core.run_turn(message, ctx, session_id)
    history = history + [
        {"role": "user", "content": message},
        {"role": "assistant", "content": reply},
    ]
    return history, ""


with gr.Blocks(title="Automated Attendance Report Generator") as demo:
    gr.Markdown(
        "# 📋 Automated Attendance Report Generator\n"
        "An agentic assistant (OpenAI Agents SDK + Groq) that analyzes an attendance "
        "sheet, writes a report, answers follow-up questions, and remembers past "
        "uploads across sessions."
    )

    csv_state = gr.State(None)
    session_id_box = gr.Textbox(
        label="Session ID (memory key -- reuse it to resume a previous conversation)",
        value=str(uuid.uuid4()),
    )

    with gr.Row():
        with gr.Column(scale=1):
            with gr.Tab("📂 Upload CSV"):
                file_in = gr.File(
                    label="Drop or click to upload attendance CSV",
                    file_types=[".csv"],
                    file_count="single",
                )
                upload_btn = gr.Button("Upload & Preview", variant="secondary")
            with gr.Tab("📋 Sample Data"):
                gr.Markdown(
                    "Load the bundled sample dataset to try out the agent without "
                    "uploading your own file (12 students, 10 sessions)."
                )
                sample_btn = gr.Button("Load Sample Data", variant="secondary")
            label_box = gr.Textbox(label="Label for this upload", value="Week 1 - CS-A")
            threshold_slider = gr.Slider(50, 100, value=75, step=1, label="Defaulter threshold (%)")
            upload_status = gr.Markdown()
            preview_df = gr.Dataframe(label="Data Preview", interactive=False)

        with gr.Column(scale=2):
            with gr.Tab("Generate Report"):
                gen_btn = gr.Button("Generate Report", variant="primary")
                report_out = gr.Markdown()
                file_out = gr.File(label="Download report")
            with gr.Tab("Ask Questions"):
                chatbot = gr.Chatbot(height=400)
                msg_box = gr.Textbox(label="Ask about the attendance data", placeholder="e.g. Who is below 75%?")
                send_btn = gr.Button("Send")

    file_in.change(handle_upload, inputs=[file_in], outputs=[csv_state, preview_df, upload_status])
    upload_btn.click(handle_upload, inputs=[file_in], outputs=[csv_state, preview_df, upload_status])
    sample_btn.click(load_sample, outputs=[csv_state, preview_df, upload_status])

    gen_btn.click(
        generate_report,
        inputs=[csv_state, threshold_slider, session_id_box, label_box],
        outputs=[report_out, file_out],
    )

    chat_inputs = [msg_box, chatbot, csv_state, threshold_slider, session_id_box, label_box]
    send_btn.click(chat_fn, inputs=chat_inputs, outputs=[chatbot, msg_box])
    msg_box.submit(chat_fn, inputs=chat_inputs, outputs=[chatbot, msg_box])

if __name__ == "__main__":
    demo.launch()
