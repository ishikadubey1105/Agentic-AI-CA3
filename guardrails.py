"""
One input guardrail: a small, cheap agent classifies whether the incoming
message is actually about attendance analysis/reporting. If not (politics,
random chit-chat, unrelated coding help, ...) it trips and the main pipeline
never runs -- the same "boundary" pattern taught for things like a politics
guardrail, just applied to keep this assistant on-topic.
"""
from pydantic import BaseModel

from agents import Agent, GuardrailFunctionOutput, RunContextWrapper, Runner

from config import MODEL


class TopicCheck(BaseModel):
    is_attendance_related: bool
    reasoning: str


topic_guardrail_agent = Agent(
    name="Topic Guardrail",
    instructions=(
        "Decide whether the user's message is about classroom attendance analysis or "
        "reporting -- e.g. asking for a report, defaulters, attendance percentages, "
        "session trends, or comparisons across uploads. Mark is_attendance_related=False "
        "for anything else (politics, general chit-chat, unrelated coding help, etc.)."
    ),
    output_type=TopicCheck,
    model=MODEL,
)


async def attendance_topic_guardrail(
    ctx: RunContextWrapper, agent: Agent, input_data: str
) -> GuardrailFunctionOutput:
    result = await Runner.run(topic_guardrail_agent, input_data, context=ctx.context)
    check: TopicCheck = result.final_output
    return GuardrailFunctionOutput(
        output_info=check,
        tripwire_triggered=not check.is_attendance_related,
    )
