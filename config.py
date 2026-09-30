"""
Central place that wires the OpenAI Agents SDK to Groq's OpenAI-compatible
endpoint. Import this module first (app.py does) so the SDK-wide client is
set before any Agent is created.
"""
import os

from dotenv import load_dotenv
from openai import AsyncOpenAI
from agents import set_default_openai_client, set_default_openai_api, set_tracing_disabled

load_dotenv()

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
DB_PATH = os.getenv("ATTENDANCE_DB_PATH", "attendance_memory.db")

if not GROQ_API_KEY:
    raise RuntimeError(
        "GROQ_API_KEY is not set. Copy .env.example to .env and paste your Groq key in."
    )

_client = AsyncOpenAI(base_url="https://api.groq.com/openai/v1", api_key=GROQ_API_KEY)

# Groq only implements the Chat Completions shape, not OpenAI's newer Responses
# API, so the Agents SDK must be told to talk to it that way.
set_default_openai_client(_client)
set_default_openai_api("chat_completions")
# Tracing uploads run data to OpenAI's dashboard; disable it since we're not
# authenticated against OpenAI itself.
set_tracing_disabled(True)
