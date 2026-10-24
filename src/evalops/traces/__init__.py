"""Agentic trace support: normalized model, in-process capture, framework adapters.

Public API:
  AgentTrace, Span                 normalized trace model (what trajectory scorers receive)
  TRACE_SCHEMA_VERSION, SOURCE     the nested trace document's version and closed source set
  BREAKER_PARITY                   per-capture-path circuit-breaker behaviour (docs table)
  langchain_callbacks()            LangChain/LangGraph native capture (enforces the circuit breaker)
  openai_agents_processor()        OpenAI Agents SDK native capture
  check_budget()                   cooperative circuit breaker for custom loops
  BudgetExceeded                   raised when a row's limits are exceeded
OpenTelemetry-instrumented frameworks (ADK, Strands, Pydantic AI, CrewAI, LlamaIndex, AutoGen,
Semantic Kernel, smolagents, DSPy, ...) are captured automatically when `traces.capture: true`.
"""

from .capture import BREAKER_PARITY, BudgetExceeded, check_budget, current, install_otel, row_capture  # noqa: F401
from .langchain import langchain_callbacks  # noqa: F401
from .model import SOURCE, TRACE_SCHEMA_VERSION, AgentTrace, Span  # noqa: F401
from .openai_agents import openai_agents_processor  # noqa: F401
