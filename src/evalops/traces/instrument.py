"""Turn on OpenTelemetry instrumentation for popular agent frameworks by name.

evalops.yaml:
  traces:
    capture: true
    instrument: [langchain, openai]      # presets below, or any "module:Class" with .instrument()

Frameworks that emit OTel GenAI spans NATIVELY need no instrumentor, only `capture: true`
(evalops attaches its processor to the global TracerProvider): Google ADK, Strands Agents,
Semantic Kernel (enable its GenAI diagnostics), AutoGen core. Pydantic AI: preset 'pydantic_ai'.
Instrumentor class paths follow OpenInference naming; VERIFY against the package versions you install.
"""

from __future__ import annotations

import importlib

from ..util import EXIT_USAGE, EvalOpsError

OPENINFERENCE = {
    "langchain": "openinference.instrumentation.langchain:LangChainInstrumentor",  # LangChain + LangGraph
    "llama_index": "openinference.instrumentation.llama_index:LlamaIndexInstrumentor",
    "crewai": "openinference.instrumentation.crewai:CrewAIInstrumentor",
    "openai_agents": "openinference.instrumentation.openai_agents:OpenAIAgentsInstrumentor",
    "google_adk": "openinference.instrumentation.google_adk:GoogleADKInstrumentor",  # alt. to ADK native spans
    "smolagents": "openinference.instrumentation.smolagents:SmolagentsInstrumentor",
    "dspy": "openinference.instrumentation.dspy:DSPyInstrumentor",
    "autogen_agentchat": "openinference.instrumentation.autogen_agentchat:AutogenAgentChatInstrumentor",
    "openai": "openinference.instrumentation.openai:OpenAIInstrumentor",
    "anthropic": "openinference.instrumentation.anthropic:AnthropicInstrumentor",
    "bedrock": "openinference.instrumentation.bedrock:BedrockInstrumentor",
    "vertexai": "openinference.instrumentation.vertexai:VertexAIInstrumentor",
    "litellm": "openinference.instrumentation.litellm:LiteLLMInstrumentor",
    "mcp": "openinference.instrumentation.mcp:MCPInstrumentor",
}
NATIVE = {"google_adk_native", "strands", "semantic_kernel", "autogen_core"}  # capture only, nothing to call

_done: set[str] = set()


def _pydantic_ai():
    from pydantic_ai import Agent

    Agent.instrument_all()


SPECIAL = {"pydantic_ai": _pydantic_ai}


def instrument(names: list[str]) -> list[str]:
    enabled = []
    for name in names or []:
        if name in _done:
            continue
        if name in NATIVE:
            _done.add(name)
            enabled.append(name)
            continue
        try:
            if name in SPECIAL:
                SPECIAL[name]()
            else:
                spec = OPENINFERENCE.get(name, name)
                if ":" not in spec:
                    raise EvalOpsError(
                        f"unknown instrumentation preset '{name}' (have: "
                        f"{sorted(OPENINFERENCE) + sorted(SPECIAL) + sorted(NATIVE)})",
                        EXIT_USAGE,
                    )
                module, cls = spec.split(":", 1)
                getattr(importlib.import_module(module), cls)().instrument()
        except ImportError as e:
            raise EvalOpsError(
                f"instrumentation '{name}' needs a package that is not installed: {e}", EXIT_USAGE
            ) from e
        _done.add(name)
        enabled.append(name)
    return enabled
