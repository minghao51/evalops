# Wiring other frameworks (snippets; not run in evalops' CI)

**Google ADK.** ADK emits OpenTelemetry spans itself.
```yaml
traces: {capture: true}      # and: pip install opentelemetry-sdk
```
```python
def predict(question: str) -> str:
    events = runner.run(user_id="eval", session_id=new_session(), new_message=to_content(question),
                        run_config=RunConfig(max_llm_calls=20))
    return final_text(events)
```

**OpenAI Agents SDK.**
```python
import agents, evalops.traces as et
agents.add_trace_processor(et.openai_agents_processor())      # once at import time
async def predict(question: str) -> str:                       # async predict is supported
    return (await agents.Runner.run(triage_agent, question, max_turns=10)).final_output
```

**CrewAI / LlamaIndex / smolagents / DSPy** (OpenInference instrumentors).
```yaml
traces: {capture: true, instrument: [crewai, openai]}   # pip install openinference-instrumentation-crewai ...
```

**Pydantic AI.**
```yaml
traces: {capture: true, instrument: [pydantic_ai]}
```
```python
result = await agent.run(question, usage_limits=UsageLimits(request_limit=10))
```

**Custom agent loop.**
```python
for step in range(max_steps):
    evalops.traces.check_budget()            # cooperative circuit breaker
    ...                                      # instrument your LLM client: traces.instrument: [openai]
```
