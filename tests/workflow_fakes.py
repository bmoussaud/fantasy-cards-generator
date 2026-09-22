"""Offline Agent seam for fault injection; the workflow scheduler remains real."""

import json
from types import SimpleNamespace

from agent_framework import AgentContext, AgentResponse, AgentSession, Message

from hosted_agents.card_orchestrator.specialists import SpecialistResult
from hosted_agents.card_orchestrator.workflow import StageBoundary


class ResultBoundary(StageBoundary):
    def __init__(self, boundary, respond):
        super().__init__(boundary.state, boundary.stage)
        self.respond = respond

    async def invoke(self, context, call_next):
        return await self.respond(self.stage, json.loads(context.messages[0].text))


class OfflineAgent:
    description = None

    def __init__(self, client, *, name, instructions, middleware, default_options):
        self.name = name
        self.instructions = instructions
        self.middleware = middleware
        self.default_options = default_options
        self.client = client

    def create_session(self):
        return AgentSession()

    async def run(self, messages, **kwargs):
        context = AgentContext(
            agent=self,
            messages=messages,
            **{key: value for key, value in kwargs.items() if key in {"session", "stream"}},
        )

        async def invoke():
            stage = self.name.removeprefix("card_")
            result = await self.client(stage, json.loads(messages[0].text))
            if isinstance(result, SpecialistResult):
                # Fault injection of closed outcomes, not a model-authored control plane.
                from hosted_agents.card_orchestrator.orchestrator import _Stop

                if result.status != "completed":
                    if result.status not in {"refused", "held", "routing_defer"}:
                        raise _Stop("held", "invalid_stage_status")
                    raise _Stop(result.status, "stage_not_completed")
                text = result.text
            else:
                text = json.dumps(result)
            context.result = AgentResponse(
                messages=[Message("assistant", [text])],
                finish_reason="stop",
                raw_representation=SimpleNamespace(
                    raw_representation=SimpleNamespace(error=None, status="completed")
                ),
            )

        await self.middleware[0].process(context, invoke)
        return context.result
