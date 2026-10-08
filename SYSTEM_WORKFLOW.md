# AI Assistant System Architecture & Workflow

> **Purpose:** This document is the authoritative architecture and workflow specification for the AI assistant.  
> Coding/AI agents modifying the system MUST follow these rules unless a higher-priority project requirement explicitly overrides them.

---

## 1. Architecture Summary

The system uses **one central Orchestrator on the Laptop**.

The Laptop is the **control plane**:

- User interface
- Input/output handling
- Session and conversation context
- Service discovery and health state
- Routing policy
- Workflow execution
- Timeout/retry/fallback
- Tool authorization and validation

The Jetson and Cloud are **AI inference backends**. The Laptop also owns handbook retrieval and the vehicle/tool gateway.

```text
                         ┌──────────────────────────┐
                         │          LAPTOP           │
                         │                          │
User ──> UI / Input ───> │      ORCHESTRATOR        │
                         │                          │
                         │ Session / Context        │
                         │ Routing / Policy         │
                         │ Health / Capability      │
                         │ Retry / Fallback         │
                         │ Tool Validation          │
                         └────────────┬─────────────┘
                                      │
                     ┌────────────────┴────────────────┐
                     │                                 │
                    LAN                            Internet
                     │                                 │
          ┌──────────▼──────────┐           ┌──────────▼──────────┐
          │   LOCAL BACKEND     │           │   CLOUD BACKEND     │
          │      Jetson         │           │                     │
          │                     │           │ Cloud STT           │
          │ Local STT           │           │ Cloud LLM           │
          │ Local LLM / SLM     │           │ Cloud TTS           │
          │ Local TTS           │           │                     │
          │ No session/tools    │           │                     │
          │ No vehicle access   │           └─────────────────────┘
          └─────────────────────┘

          Laptop: handbook SQLite/RAG + safety/confirmation + vehicle/MQTT
```

### Core principle

```text
Laptop API = control plane, canonical session, RAG and tool gateway
Jetson = optional, stateless local AI inference server
Cloud = optional online AI inference backend
```

There must be **exactly one main Orchestrator**.

The Jetson MUST NOT contain a second independent system-level Orchestrator.

---

# 2. Design Goals

The architecture is designed around the following goals:

1. **Single source of workflow control**
2. **Backend independence**
3. **Optional Jetson integration**
4. **Graceful degradation**
5. **Explicit service capabilities**
6. **Deterministic routing**
7. **Safe tool execution**
8. **Consistent module interfaces**
9. **Session continuity across backend changes**
10. **Easy addition/removal of local or cloud services**

The system must be able to operate with different combinations of available backends without requiring architectural changes to the Orchestrator.

---

# 3. Component Responsibilities

## 3.1 Laptop

The Laptop owns:

- UI
- User input
- User output
- Session state
- Conversation history
- Request IDs
- Service registry
- Health state
- Capability state
- Routing policy
- Workflow execution
- Timeout/retry/fallback
- Tool validation
- Canonical handbook SQLite index, local query embedding/FTS retrieval, evidence checking and citations
- Vehicle gateway, confirmation and MQTT access

The Laptop API MUST remain the owner of the session even when inference moves to Jetson. The Browser owns only interaction state and carries `session_id`/`turn_id`; it is not the canonical session store.

---

## 3.2 Orchestrator

The Orchestrator is the central workflow controller.

It decides:

- Which processing stages are required
- Which backend should perform each stage
- Whether a service is available
- Whether a service is healthy
- Whether a tool may be executed
- Whether retry is appropriate
- Whether fallback is possible
- When the request is complete

The Orchestrator is primarily **deterministic application code**.

An LLM is NOT required just to perform routing or health decisions.

---

## 3.3 Local Backend

The proposed Local Backend is an AI inference server on Jetson AGX Xavier. This is a target topology, not a claim that the current repository already deploys such a server. A standalone Nano profile running the entire API is a separate deployment mode.

The Jetson should be treated as a **plug-in backend**.

The Laptop does not hard-code the existence of a Jetson.

Instead, the Orchestrator discovers:

```text
Backend exists?
    ↓
Backend reachable?
    ↓
Backend capabilities?
    ↓
Required capability available?
    ↓
Service healthy?
    ↓
Policy allows local execution?
```

Only then may the Orchestrator route work to the Local Backend.

### Local Backend may provide

- Local STT
- Local LLM / SLM
- Local TTS

Not every Jetson installation must provide every capability.

Jetson MUST NOT own conversation state, the handbook database, RAG retrieval/grounding, vehicle tools, MQTT access, confirmation or safety policy. Its model may return a structured proposal, but only the Laptop can authorize and execute it.

---

## 3.4 Cloud Backend

The Cloud Backend provides online services such as:

- Cloud STT
- Cloud LLM
- Cloud TTS

Cloud services are accessed directly by the Laptop Orchestrator.

Jetson is not required for Cloud access.
External APIs and tools, including web search when enabled, are invoked through Laptop adapters under the same policy boundary; an AI provider does not execute them on ViVi's behalf.

---

# 4. Backend as a Plug-in

The Orchestrator should interact with **backend capabilities**, not hardware-specific assumptions.

Bad:

```python
if jetson_available:
    use_jetson()
```

Preferred:

```python
backend = backend_registry.select(
    capability="llm",
    policy=current_policy
)

result = backend.llm(request)
```

The Orchestrator should conceptually see:

```text
Backend
├── identity
├── status
├── capabilities
├── health
└── service interfaces
```

The physical implementation may be:

```text
Jetson
Cloud
Future edge device
Future local server
```

The workflow should not need to change when a new backend is added.

---

# 5. Capability Discovery

For one known Jetson, configure its address on Laptop and probe it. Automatic LAN discovery is optional; it is not required to implement plug-and-play behavior. The Jetson AI service should expose a versioned capability/readiness interface.

Example:

```json
{
  "contract_version": "1.0",
  "backend_id": "jetson-xavier",
  "status": "reachable",
  "checked_at": "2026-10-09T00:00:00Z",
  "capabilities": {
    "stt": {"status": "ready", "model_id": "local-whisper", "streaming": false},
    "llm": {"status": "ready", "model_id": "local-slm", "streaming": true},
    "tts": {"status": "loading", "model_id": "local-tts", "streaming": true}
  },
  "queue_depth": 0
}
```

This information is **descriptive**, not a direct routing command.

The Orchestrator remains responsible for deciding whether to use the capability.

### Capability states

A capability should conceptually have states such as:

```text
UNKNOWN
AVAILABLE
DEGRADED
UNAVAILABLE
```

These are Laptop registry states. The Jetson wire status (`ready`, `loading`, `busy`, `unavailable`) is mapped into them; `loading` and `busy` are not advertised as `AVAILABLE` for a new call.

Do not treat:

```text
backend online
```

as equivalent to:

```text
all backend services healthy
```

A Jetson can be reachable while its LLM is loading, busy or unavailable. `GET /health/live` checks the process; `GET /v1/capabilities` reports per-model readiness. A ready result has a short Laptop-side TTL and is rechecked after transport failures.

---

# 6. Health Monitoring

Health monitoring is separate from business workflow.

Conceptually:

```text
Health Monitor
      ↓
Service Registry
      ↓
Orchestrator
      ↓
Routing Policy
```

The system should track:

```text
Backend:
    reachable
    last_success
    last_failure

Capability:
    available
    healthy
    last_check

Service:
    timeout
    error state
    retry state
```

The exact implementation is project-specific, but the separation of responsibilities must remain.

---

# 7. Routing Policy

Routing is determined by:

1. Required capability
2. Backend availability
3. Backend health
4. Current connectivity
5. System policy
6. Request requirements
7. Fallback options
8. Remaining turn deadline and backend queue/load

The LLM must not decide connectivity state.

The LLM may help determine **what the user wants**, but the Orchestrator determines **where/how the request is executed**. Cloud use must obey explicit deployment policy; loss of Jetson must not silently override an offline-only setting.

---

# 8. Four Main Operating Modes

These are user-visible capability summaries, not four separate orchestration implementations. The Router selects each inference stage from capability readiness, policy and remaining deadline. Laptop handbook retrieval, safety and vehicle access do not depend on Jetson. The matrix assumes the Laptop has the handbook artifact, local retrieval dependencies and rules but no production STT, TTS or SLM of its own. If embedding inference on Laptop fails, FTS5-only retrieval remains available; missing/corrupt handbook artifacts produce an explicit RAG-unavailable result.

| Jetson | Internet | Primary mode |
|---|---|---|
| UP | UP | Jetson or cloud inference + Laptop RAG/tools |
| DOWN | UP | Cloud inference + Laptop RAG/tools |
| UP | DOWN | Jetson inference + Laptop RAG/tools |
| DOWN | DOWN | Laptop text + rules/extractive RAG/tools only |

---

# 9. Mode 1 — Jetson UP + Internet UP

This is the normal full-capability mode. Preference for Jetson or cloud is a deployment policy, not an implicit hardware rule.

Typical route:

```text
User
 ↓
Laptop UI
 ↓
Orchestrator
 ↓
Input normalization
 ↓
Selected Jetson/Cloud STT (if audio)
 ↓
Laptop RAG / Context
 ↓
Selected Jetson/Cloud LLM
 ↓
Laptop safety/confirmation/tool execution if required
 ↓
Deterministic response or selected LLM finalization
 ↓
Selected Jetson/Cloud TTS (if audio)
 ↓
Laptop output
```

Jetson can provide:

```text
Local STT
Local LLM
Local TTS
```

The exact selection is policy-dependent.

### Important

Jetson being available does NOT mean Jetson must always be used.

For example:

```text
Internet UP
Jetson UP
Cloud LLM preferred
```

is valid.

The Local Backend remains an available backend/fallback.

---

# 10. Mode 2 — Jetson DOWN + Internet UP

The Orchestrator continues normally.

Jetson-dependent inference services are removed from the available route. Laptop handbook retrieval and vehicle tools remain available. If cloud use is disallowed or cloud LLM fails, Laptop can still use rules/extractive generation from retrieved evidence.

Typical route:

```text
User
 ↓
Laptop
 ↓
Orchestrator
 ↓
Cloud STT
 ↓
Laptop handbook retrieval and evidence validation
 ↓
Cloud LLM
 ↓
Laptop safety/confirmation/tool gateway when needed
 ↓
Cloud TTS
 ↓
Laptop
```

Unavailable:

```text
Local STT
Local LLM / SLM
Local TTS
```

The failure of Jetson MUST NOT terminate the entire assistant.

---

# 11. Mode 3 — Jetson UP + Internet DOWN

The Jetson becomes the primary AI backend.

Typical route:

```text
User
 ↓
Laptop
 ↓
Orchestrator
 ↓
Local STT
 ↓
Laptop handbook retrieval
 ↓
Local LLM / SLM
 ↓
Laptop safety/confirmation/tool gateway when needed
 ↓
Local LLM / SLM
 ↓
Local TTS
 ↓
Laptop
```

No Cloud service should be required.

Unavailable:

```text
Cloud STT
Cloud LLM
Cloud TTS
```

The Laptop still acts as the Orchestrator.

Internet failure does NOT move orchestration to Jetson.

---

# 12. Mode 4 — Jetson DOWN + Internet DOWN

The system enters restricted mode.

Current architecture does not assume an SLM on the Laptop.

Therefore:

```text
Audio input → unavailable
Cloud LLM → unavailable
Local LLM → unavailable
Cloud TTS → unavailable
Local TTS → unavailable
```

The system should support:

```text
Text input
 ↓
Rule-based handling
 ↓
Laptop E5 + FTS5 retrieval, or FTS5-only if E5 is unavailable
 ↓
Rules/extractive answer with citations, or explicit abstention
 ↓
Laptop UI
```

The system must explicitly indicate degraded operation. Losing Jetson does not disable handbook RAG: retrieval and source validation run on Laptop; only model-based answer generation is unavailable in this mode. Vehicle actions can still be proposed by deterministic rules and executed only through the Laptop safety/confirmation gateway when the vehicle adapter is available. Handbook answers remain subject to evidence checks.

It must not pretend that an LLM is available.

---

# 13. Request Lifecycle

A request follows this conceptual lifecycle:

```text
RECEIVED
   ↓
NORMALIZED
   ↓
CONTEXT_LOADED
   ↓
ROUTE_SELECTED
   ↓
PROCESSING
   ↓
TOOL_EXECUTION (optional)
   ↓
PROCESSING
   ↓
RESPONSE_READY
   ↓
TTS (optional)
   ↓
COMPLETED
```

Failure path:

```text
PROCESSING
   ↓
SERVICE_ERROR
   ↓
RETRY?
 ┌─┴──────────┐
YES           NO
 ↓             ↓
RETRY       FALLBACK?
              │
          ┌───┴───┐
         YES      NO
          ↓        ↓
      FALLBACK   DEGRADED
          ↓      RESPONSE
      PROCESSING
```

---

# 14. Module Communication Rules

This section defines the mandatory communication rules between modules.

## Rule 1 — Modules communicate through explicit contracts

Every module must expose a defined input/output contract.

Do not rely on:

- hidden global state
- implicit variables
- undocumented side effects
- parsing human-readable logs
- assumptions about another module's internal implementation

---

## Rule 2 — One responsibility per module

Examples:

```text
STT      → audio → text
RAG      → query → context/documents
LLM      → messages/context → model response
TTS      → text → audio
Tool     → validated arguments → tool result
Health   → service checks → health state
Router   → requirements + availability → route
```

A module should not silently perform another module's responsibility.

---

## Rule 3 — The Orchestrator owns sequencing

Modules may perform their task.

They should not independently decide the global next step.

Correct:

```text
Orchestrator
    ↓
STT
    ↓
Orchestrator
    ↓
LLM
    ↓
Orchestrator
    ↓
Tool
```

Avoid uncontrolled:

```text
STT → LLM → Tool → TTS
```

where each module independently invokes the next module.

---

## Rule 4 — No hidden module-to-module dependencies

A module must not silently instantiate or call another module unless that dependency is explicitly part of its contract.

For example, STT should not silently call the LLM.

RAG should not silently call Cloud APIs unless explicitly defined as part of the RAG backend.

---

## Rule 5 — Communication must be typed/structured

Prefer structured requests and responses.

Example request:

```json
{
  "contract_version": "1.0",
  "request_id": "req-123",
  "call_id": "call-456",
  "attempt": 1,
  "capability": "llm",
  "timeout_ms": 8000,
  "input": {
    "text": "..."
  },
  "context": []
}
```

Example response:

```json
{
  "contract_version": "1.0",
  "request_id": "req-123",
  "call_id": "call-456",
  "attempt": 1,
  "status": "success",
  "result": {},
  "error": null,
  "backend_id": "jetson-xavier",
  "model_id": "local-slm",
  "latency_ms": 247
}
```

The exact schema may evolve, but the following fields should remain conceptually available:

```text
request_id
call_id
contract_version
status
result
error
backend_id
```

---

# 15. Request ID and Session ID

Every public turn should carry:

```text
session_id
turn_id
```

The Laptop creates or retrieves one `request_id` at the first public ingress for `(session_id, turn_id)` (STT for voice, `/turn` for text) and reuses it for later turn/TTS calls. It uses this as the trace correlation ID. Each inference stage has its own `call_id`; each retry/fallback increments `attempt`. This extends the existing public `session_id`/`turn_id` contract without requiring the Browser to invent new IDs.

### request_id

Identifies one end-to-end user turn on the Laptop. The same value survives stage retries and backend changes.

Useful for:

- logging
- tracing
- retry
- debugging
- correlating tool calls

### session_id

Identifies the conversation/session.

It must survive:

```text
Cloud → Local
Local → Cloud
Jetson reconnect
Fallback
Retry
```

Changing backend must NOT create a new session.

### turn_id, call_id and attempt

`turn_id` identifies the user turn within a session. `call_id` identifies one logical STT, LLM or TTS operation within that turn. `attempt` identifies a concrete try against one backend. Retrying an AI call creates a new attempt, never a new user turn. Vehicle command IDs and idempotency keys are separate and MUST NOT be regenerated merely because an AI call is retried.

---

# 16. Context Ownership

The Laptop owns the canonical session context.

Services receive only the context required for their task. Jetson does not need the canonical `session_id` or a persistent copy of history; a Laptop adapter may send bounded messages, retrieved evidence and opaque correlation IDs for one inference call.

Example:

```text
Laptop Session
     │
     ├── conversation history
     ├── request state
     ├── retrieved context
     └── tool results
             │
             ▼
          Backend
```

A backend must not become the permanent owner of the conversation. Cloud disclosure of transcript, history, vehicle state and evidence must follow the configured data policy.

---

# 17. LLM Communication Contract

The LLM is a processing module, not the system controller.

The LLM may return:

```text
final answer
tool request
clarification request
structured decision
```

The Orchestrator interprets the response.

Example:

```text
LLM
 ↓
Tool request
 ↓
Orchestrator
 ↓
Validate:
    tool exists?
    capability available?
    arguments valid?
    policy allows it?
    safety checks pass?
 ↓
Execute
 ↓
Tool result
 ↓
LLM
```

The LLM must never be trusted to bypass authorization.

---

# 18. Tool Communication Rules

Tools must have explicit contracts.

Example:

```text
Tool:
    name
    description
    input schema
    output schema
    permission/policy
    availability
```

Tool execution should be:

```text
Tool Request
    ↓
Schema validation
    ↓
Permission/policy validation
    ↓
Execution
    ↓
Structured result
```

Invalid tool arguments must fail before execution.

Unknown tools must never be executed.

---

# 19. Error Contract

Errors must be machine-readable.

Conceptually:

```json
{
  "status": "error",
  "error": {
    "code": "SERVICE_TIMEOUT",
    "message": "Local LLM request timed out",
    "retryable": true,
    "backend_id": "jetson-xavier",
    "service": "llm",
    "call_id": "call-123",
    "attempt": 1
  }
}
```

Useful error categories include:

```text
INVALID_REQUEST
INVALID_ARGUMENT
UNAUTHORIZED
SERVICE_UNAVAILABLE
SERVICE_TIMEOUT
NETWORK_ERROR
MODEL_ERROR
TOOL_ERROR
INTERNAL_ERROR
CAPABILITY_UNAVAILABLE
MODEL_LOADING
BACKEND_BUSY
DEADLINE_EXCEEDED
```

Do not use exception message text as the primary routing mechanism.

Routing should use structured error codes/state.

---

# 20. Timeout Rules

Every remote/backend request must have a bounded timeout.

The Laptop owns one monotonic deadline for the whole turn and a smaller per-stage timeout. It sends a remaining `timeout_ms` to an AI backend; it never relies on synchronized device clocks. A retry or fallback cannot extend the original turn deadline.

A timeout must:

1. Stop waiting indefinitely.
2. Produce a structured failure.
3. Update service health if appropriate.
4. Trigger retry/fallback policy.

Example:

```text
Jetson LLM
 ↓
timeout
 ↓
SERVICE_TIMEOUT
 ↓
health update
 ↓
retry?
 ↓
fallback?
```

A timeout in one module must not freeze the whole application indefinitely.

---

# 21. Retry Rules

Retries must be bounded.

Only inference/read operations may be retried automatically. A vehicle command is handled under its own idempotency and ACK/read-back contract, never by the inference retry loop.

A retry policy should define:

```text
max_attempts
timeout
backoff
retryable_errors
```

Do not retry errors that are clearly non-retryable, such as:

```text
INVALID_ARGUMENT
UNAUTHORIZED
UNKNOWN_TOOL
```

Retry may be appropriate for:

```text
TEMPORARY_NETWORK_ERROR
SERVICE_TIMEOUT
TRANSIENT_SERVICE_UNAVAILABLE
```

---

# 22. Fallback Rules

Fallback is controlled by the Orchestrator.

Example:

```text
Cloud LLM
 ↓
failure
 ↓
Is Local LLM available?
 ↓
YES
 ↓
Policy allows fallback?
 ↓
YES
 ↓
Local LLM
```

Fallback must preserve:

```text
session_id
request_id
turn_id
conversation context
task state
```

Do not silently restart the conversation.

Fallback is allowed only before a stage's output is committed to the user or a side effect is issued. If audio or text has already been emitted, the Laptop must stop that output and explicitly restart or report failure; it must not splice output from different models into one answer. After a vehicle command has been sent, use ACK/read-back to determine its result; never replay the command because an AI backend failed.

---

# 23. Backend Selection

Backend selection should be abstract.

Conceptually:

```python
backend = router.select_backend(
    task="llm",
    required_capabilities=["llm"],
    available_backends=registry,
    policy=policy
)
```

The Router should not depend directly on:

```python
JetsonAGXXavier
```

unless the backend implementation itself requires that hardware-specific class.

The upper-level workflow should depend on:

```text
Backend interface
Capability interface
Service interface
```

not hardware names.

---

# 24. Local Backend Lifecycle

Jetson may appear or disappear during runtime.

Expected lifecycle of each advertised AI capability:

```text
NOT_DISCOVERED
      ↓
DISCOVERED
      ↓
CONNECTING
      ↓
AVAILABLE
      ↓
DEGRADED
      ↓
UNAVAILABLE
      ↓
RECONNECTING
      ↓
AVAILABLE
```

When Jetson reconnects:

1. Probe the configured backend endpoint (or discover an endpoint if discovery is enabled).
2. Read capabilities.
3. Perform health checks.
4. Update registry.
5. Make capabilities available to routing policy.
6. Do NOT automatically interrupt an active request unless explicitly required.
7. Future requests may use the newly available backend.

---

# 25. Plug-and-Play Principle

The system should behave as if the Jetson is an optional device.

### Jetson connected

```text
Capabilities:
    local_stt
    local_llm
    local_tts
```

These capabilities become available.

### Jetson disconnected

They become unavailable in the service registry. Laptop RAG, rules, safety and vehicle access remain independent.

The Orchestrator continues using remaining capabilities.

Therefore:

```text
Connect Jetson
    → capabilities added

Disconnect Jetson
    → capabilities removed

No Orchestrator rewrite required.
```

---

# 26. RAG Communication

Laptop RAG should expose a retrieval contract to the Laptop Orchestrator. Its handbook SQLite artifact, FTS5 index and query embedding model are stored and executed on Laptop. FTS5-only retrieval is the required fallback if the embedding model is unavailable. No network call to Jetson or cloud may be required to retrieve and validate handbook evidence.

Input:

```text
query
optional filters
vehicle model/year/locale and handbook artifact version
```

Output:

```text
documents/chunks with stable source IDs
metadata
scores if available
source information
```

Example:

```json
{
  "query": "user query",
  "artifact_version": "handbook-v1",
  "results": [
    {
      "text": "...",
      "source_id": "vf8-2026-section-123",
      "source_url": "...",
      "section_path": ["..."],
      "metadata": {"vehicle_model": "VF8", "model_year": 2026, "locale": "vi_vn"}
    }
  ]
}
```

RAG should not directly generate the final answer unless explicitly designed as a combined component.

The normal flow is:

```text
Orchestrator
 ↓
RAG
 ↓
Context
 ↓
LLM
```

The Laptop validates evidence and citations after generation. The selected Jetson/cloud LLM receives only accepted evidence needed for the answer; it does not determine source IDs or query the handbook store directly. With neither LLM available, Laptop uses rules/extractive composition from accepted evidence and cites those sources or abstains. Answer quality may be lower, but retrieval remains functional.

---

# 27. STT Communication

STT input:

```text
audio
audio metadata
request_id
```

STT output:

```text
text
confidence if available
language if available
metadata
```

STT should not:

- invoke the LLM
- select tools
- manage conversation state
- decide final routing

---

# 28. TTS Communication

TTS input:

```text
text
voice/configuration
request_id
```

TTS output:

```text
audio
metadata
```

TTS should not decide the response content.

The Orchestrator decides whether TTS is required.

---

# 29. Output Handling

All final results return to the Laptop.

```text
Backend
 ↓
Orchestrator
 ↓
Output layer
 ↓
User
```

The Jetson should not directly control the Laptop UI.

The Jetson may produce audio/data, but the Orchestrator owns how the final output is delivered.

---

# 30. Observability

Every major workflow stage should be traceable.

At minimum, log:

```text
request_id
session_id
timestamp
module
backend
service
operation
status
latency
error_code if any
```

Example:

```text
req-123
  Orchestrator → ROUTE_SELECTED → cloud
  Cloud STT    → SUCCESS          120ms
  RAG          → SUCCESS           35ms
  Cloud LLM    → TOOL_REQUEST     800ms
  Tool         → SUCCESS           50ms
  Cloud LLM    → SUCCESS          900ms
  Cloud TTS    → SUCCESS          300ms
```

Do not expose sensitive user data unnecessarily in logs.

---

# 31. Concurrency

Modules should not assume that only one request exists.

The architecture should use `request_id` to distinguish concurrent requests.

Shared state must be explicitly managed.

A request must not accidentally consume:

- another request's context
- another request's tool result
- another request's audio
- another request's response

---

# 32. State Ownership Rules

Each state has one clear owner.

| State | Owner |
|---|---|
| UI state | Laptop UI |
| Session state | Laptop |
| Workflow state | Orchestrator |
| Backend registry | Orchestrator/system runtime |
| Health state | Health Monitor |
| Model state | Backend |
| Tool execution state | Tool/Orchestrator |
| Final output state | Laptop |

Avoid multiple modules independently modifying the same state.

---

# 33. Security / Authorization Boundary

The Orchestrator is the primary authorization boundary for tools.

Before a tool executes:

```text
LLM proposal
 ↓
Tool exists?
 ↓
Arguments valid?
 ↓
Capability available?
 ↓
Policy allows?
 ↓
Safety requirements satisfied?
 ↓
Execute
```

A backend must not assume that an LLM-generated request is authorized.

---

# 34. What Must Not Be Done

Do NOT:

1. Create a second main Orchestrator on Jetson.
2. Move session ownership to Jetson.
3. Let the LLM directly execute arbitrary tools.
4. Hard-code all workflows around the Jetson.
5. Assume Internet is always available.
6. Assume Jetson is always available.
7. Use service logs as an API contract.
8. Use unbounded retries.
9. Wait forever for a failed backend.
10. Mix session state with backend state.
11. Make STT directly invoke LLM.
12. Make RAG directly control workflow.
13. Make TTS decide response logic.
14. Make one module silently call unrelated modules.
15. Create hidden global state shared between requests.
16. Change the backend without preserving session/request context.
17. Treat `backend_online` as equivalent to `all_services_healthy`.
18. Pretend that unavailable AI capabilities exist in degraded mode.
19. Add a new backend by modifying the core workflow unnecessarily.
20. Use hardware-specific logic in the high-level routing layer when a capability abstraction is sufficient.

---

# 35. Recommended Layering

The system should conceptually be separated into these layers:

```text
┌──────────────────────────────────────────────┐
│                 PRESENTATION                 │
│ UI / microphone / speaker / user interaction │
└──────────────────────┬───────────────────────┘
                       │
┌──────────────────────▼───────────────────────┐
│                 ORCHESTRATION                │
│ workflow / routing / session / policy        │
└──────────────────────┬───────────────────────┘
                       │
┌──────────────────────▼───────────────────────┐
│                 SERVICE LAYER                │
│ STT / RAG / LLM / TTS / Tools                │
└──────────────────────┬───────────────────────┘
                       │
┌──────────────────────▼───────────────────────┐
│                 BACKEND LAYER                │
│ Cloud / Jetson / future edge backends        │
└──────────────────────────────────────────────┘
```

The upper layers should depend on interfaces/contracts rather than concrete backend implementations.

---

# 36. Example End-to-End Request

Assume:

```text
Jetson = online
Internet = online
User input = voice
User requires an answer involving a local device
```

Workflow:

```text
1. Laptop receives audio.

2. Browser sends session_id and turn_id; Laptop creates request_id/trace_id.

3. Orchestrator selects a ready STT backend according to policy and deadline.

4. STT returns text.

5. Laptop loads session context and reads the vehicle adapter when needed.

6. Laptop RAG retrieves and validates handbook evidence when needed.

7. Laptop selects a ready LLM backend (Jetson or cloud).

8. Laptop sends a bounded inference request containing:
       user text
       relevant conversation context
       accepted evidence
   to the selected LLM.

9. LLM may propose an action; it cannot execute it.

10. Orchestrator validates:
       tool exists
       arguments valid
       policy allows execution
       vehicle state and confirmation are valid

11. Laptop gateway executes the vehicle tool via the authorized adapter.

12. Tool returns structured result.

13. Laptop may send a verified tool result to the selected LLM if generation needs it.

14. Laptop composes the final result from verified tool state or a grounded LLM answer.

15. Laptop sends safe final text to the selected TTS backend if audio is required.

16. TTS returns audio.

17. Laptop outputs text/audio to user.

18. Entire workflow is recorded under the same request_id/session_id/turn_id.
```

---

# 37. Example Jetson Disconnect During Request

```text
Jetson LLM
    ↓
times out before producing a committed result
    ↓
Laptop marks that LLM capability unhealthy
    ↓
Is another LLM allowed, ready and within the turn deadline?
    ↓
YES → new attempt on cloud / NO → rules or degraded response
    ↓
Any action proposal is validated only on Laptop
    ↓
Vehicle commands, if already sent, are resolved by ACK/read-back only
```

The system should not crash simply because a backend disappeared.

---

# 38. Example Jetson Reconnect

```text
Jetson disconnected
    ↓
Local capabilities unavailable
    ↓
Orchestrator uses Cloud/Laptop fallback
    ↓
Jetson reconnects
    ↓
Probe configured endpoint (or optional LAN discovery)
    ↓
Capability registration
    ↓
Health check
    ↓
Local capabilities become AVAILABLE
    ↓
Future requests may use Local Backend
```

The system should not unnecessarily restart the entire application.

---

# 39. Architecture Invariant

The following invariant must always hold:

```text
                    ONE SYSTEM CONTROLLER

                           Laptop
                       Orchestrator
                            │
             ┌──────────────┴──────────────┐
             │                             │
        Local Backend                 Cloud Backend
           Jetson                         Cloud
             │                             │
       specialized                   specialized
        services                      services
```

The backend may change.

The available capabilities may change.

The routing policy may change.

The Orchestrator remains the central owner of workflow state.

---

# 40. Implementation Guidance for AI Coding Agents

When modifying the codebase:

### Before changing code

Determine:

1. Which layer does this code belong to?
2. Which module owns the state?
3. Which interface does the module implement?
4. Is the dependency explicit?
5. What happens if the backend is unavailable?
6. What is the timeout behavior?
7. What is the retry behavior?
8. What is the fallback?
9. Does the change preserve request/session IDs?
10. Does the change preserve the single-Orchestrator architecture?

### When adding a new service

Implement:

```text
Service interface
Capability declaration
Health check
Structured request
Structured response
Error contract
Timeout
```

### When adding a new backend

Implement:

```text
Backend identity
Capability discovery
Health reporting
Service adapters
Connection lifecycle
```

Do not modify the core Orchestrator more than necessary.

---

# 41. Source Architecture Mapping

The chosen Laptop + Jetson target architecture defines:

- Laptop as UI/session/orchestration layer.
- Jetson as local AI inference service provider; the Laptop retains RAG and tool execution.
- Cloud as online service provider.
- Four connectivity modes.
- Laptop handbook retrieval as a normal capability; rules/extractive answering is a degraded generation option.
- No SLM fallback on Laptop in the current design.
- LLM tool proposals being validated/executed by the Orchestrator.
- Health monitoring, timeout, retry, and fallback as Orchestrator responsibilities.

This document extends that architecture by formalizing the Jetson as an **optional, stateless AI inference backend probed through capability/readiness information**, and by defining communication contracts between modules.

Where implementation details are not specified by the original architecture, this document treats the described interfaces and rules as architectural requirements for implementation rather than claims about an already-existing implementation.

---

# 42. Final Mental Model

The entire system can be remembered as:

```text
User ↔ Browser ↔ Laptop API / Orchestrator
                         ├── Session + context + history
                         ├── Handbook RAG + evidence/citations
                         ├── Safety + confirmation + vehicle gateway → MQTT
                         └── AI router
                              ├── Jetson AI service: STT / LLM / TTS
                              └── Cloud AI service: STT / LLM / TTS

Only model inference crosses from Laptop to Jetson or cloud.
Only the Laptop authorizes and executes vehicle/tools.
```

**The key architectural rule is:**

> **Jetson and cloud provide AI inference. The Laptop Orchestrator owns RAG, session, safety, tools and final output.**

Jetson is therefore an optional local AI backend — it can be connected, disconnected, degraded, or replaced without creating another Orchestrator or changing the ownership of the session/workflow.

---

# 43. Concrete Communication Contracts (Target Design)

This section specifies interfaces for the proposed Laptop + Jetson deployment. It is a target contract, not a statement that the current code already implements a Jetson AI service. The existing public API in [docs/contracts.md](docs/contracts.md) remains compatible while these adapters are introduced.

## 43.1 Boundaries and transport

| Boundary | Transport and format | Owner of validation and state |
|---|---|---|
| Browser ↔ Laptop API | Existing `/api/v1` HTTPS/HTTP JSON; multipart upload for STT; WebSocket for live STT; NDJSON for turn streaming; PCM/WAV for TTS | Laptop validates public input and owns canonical session, response and safety. Browser never calls Jetson or MQTT directly. |
| Laptop Orchestrator ↔ Laptop RAG/history/memory | Typed in-process interfaces; SQLite FTS5, query embedding model and history remain on Laptop | Laptop owns handbook artifact/version, source IDs, evidence gate, citations and retention. Retrieval works without Jetson or Internet; FTS5 is the fallback if embeddings are unavailable. |
| Laptop AI adapters ↔ Jetson AI service | Versioned HTTP over LAN; JSON for control/LLM; multipart or audio bytes for STT; NDJSON for LLM streaming; PCM/WAV for TTS | Jetson validates transport/model inputs and returns inference only. No session database, RAG retrieval, tool execution or vehicle connection. |
| Laptop AI adapters ↔ cloud providers | Provider-native HTTPS behind the same Laptop capability interfaces | Laptop applies cloud-use/data policy, normalizes outputs/errors, and never delegates tool authorization. |
| Laptop gateway ↔ vehicle simulator | Existing typed gateway and MQTT command/ACK/read-back contract | Laptop alone validates arguments, policy, confirmation and verified result. |

The first deployment may use a configured Jetson URL plus health probes. A discovery mechanism is optional. A thin Jetson gateway may normalize existing model servers; it must not become a second workflow controller.

## 43.2 Identity, version and common envelope

The public API keeps `session_id` and `turn_id`. Laptop creates or retrieves one `request_id` for their pair at first ingress and reuses it across voice STT, turn and TTS calls. For every AI stage, it creates a `call_id`; each physical retry/fallback has an incrementing `attempt`. These identifiers are opaque. Jetson receives `request_id` and `call_id` for diagnostics, but does not require `session_id` or the full history. The same `request_id`/`call_id` is echoed in a response; a late result from an old attempt is ignored.

Canonical request **inside the Laptop adapter layer**:

```json
{
  "contract_version": "1.0",
  "request_id": "req-123",
  "call_id": "call-llm-1",
  "attempt": 1,
  "capability": "llm",
  "timeout_ms": 8000,
  "input": {"messages": [{"role": "user", "content": "..."}]},
  "requirements": {"model": "local-slm", "stream": false}
}
```

The Laptop uses a monotonic turn deadline; `timeout_ms` is the remaining per-call budget, not a wall-clock timestamp. The Jetson gateway accepts this versioned envelope; it may translate the LLM call to an OpenAI-compatible model endpoint. Model-native output is normalized before LangGraph consumes it. Unknown major contract versions are rejected, and breaking public API changes require `/api/v2` as specified in [docs/contracts.md](docs/contracts.md).

Canonical completed result:

```json
{
  "contract_version": "1.0",
  "request_id": "req-123",
  "call_id": "call-llm-1",
  "attempt": 1,
  "backend_id": "jetson-xavier",
  "model_id": "local-slm",
  "status": "success",
  "result": {"text": "..."},
  "error": null,
  "latency_ms": 247
}
```

`result` is capability-specific. No model text is treated as authorization or proof that a vehicle action happened.

## 43.3 Jetson service surface

| Operation | Proposed wire contract | Required result |
|---|---|---|
| Liveness | `GET /health/live` | HTTP 200 if the service process can answer; says nothing about model readiness. |
| Capability readiness | `GET /v1/capabilities` | `contract_version`, `backend_id`, per-model `status`, `model_id`, streaming support, queue depth and limits. Laptop caches briefly and invalidates after failures. |
| STT | `POST /v1/stt/transcriptions`, multipart audio plus language/format and correlation headers | UTF-8 transcript, optional confidence/language, model ID and latency; no empty transcript reported as success. |
| LLM | `POST /v1/llm/generate`, JSON messages or a structured-output request | Text or a typed proposal conforming to the requested schema; unknown tools and invalid arguments are rejected by Laptop. An adapter may map this to `/v1/chat/completions`. |
| TTS | `POST /v1/tts/synthesize`, JSON text/voice/format; optional streaming | Audio bytes with `Content-Type`, sample rate, channel count and model/voice metadata. Text response remains usable if TTS fails. |

JSON calls carry the envelope in the body. Multipart STT and binary/streaming responses carry `X-ViVi-Contract-Version`, `X-ViVi-Request-Id`, `X-ViVi-Call-Id` and `X-ViVi-Attempt` headers; Laptop also sends `X-ViVi-Timeout-Ms`. Jetson echoes correlation headers. Error responses use JSON with the normalized error shape even when the successful body is audio. An NDJSON stream has one terminal `final` or `error` event; a PCM stream ends normally or fails at transport level. The public turn stream keeps the existing event names in [docs/contracts.md](docs/contracts.md).

For Jetson, `ready` means the requested model can serve a request, `loading` means it cannot yet serve, `busy` means queue/concurrency limits are reached, and `unavailable` means it cannot serve until recovery. Process liveness must not be used as model readiness. Bound input size, audio duration, generated tokens, queue depth and concurrent jobs per capability; values are profile settings measured on the target hardware. Busy responses use HTTP 429 with `Retry-After` when known; loading/unavailable use HTTP 503.

The model server is stateless across calls. It may keep weights and inference caches in memory, but no canonical conversation, handbook, confirmation or vehicle state. Restarting Jetson must not reset a Laptop session.

## 43.4 Streaming, cancellation and commitment

The Laptop public turn stream keeps the existing NDJSON contract and one authoritative `final` event. A Jetson LLM stream uses ordered NDJSON events with `call_id`, `sequence`, `type` (`delta`, `final`, `error`) and a final usage/status record. TTS streams PCM with declared sample rate, channels and format; STT streaming is optional and must declare whether interim text is provisional. Laptop cancels an upstream stream when the user cancels or the deadline expires; a late chunk is discarded using `call_id`/`attempt`.

Progress text may be shown before completion. A vehicle-success statement is committed only after gateway ACK/read-back; a handbook answer is committed only after evidence/citation validation. Once content or audio from one model has been committed to the user, fallback cannot silently splice in another model's continuation. The Laptop either explicitly restarts that response or returns a structured failure.

## 43.5 Errors, fallback and security

All adapters normalize errors to `{code, message, retryable, backend_id, capability, call_id, attempt}`. Transport HTTP status is separate from the error code. Required codes include `INVALID_REQUEST`, `UNAUTHORIZED`, `MODEL_LOADING`, `BACKEND_BUSY`, `CAPABILITY_UNAVAILABLE`, `SERVICE_TIMEOUT`, `NETWORK_ERROR`, `MODEL_ERROR` and `DEADLINE_EXCEEDED`. A retry is allowed only when the operation is safe to repeat and the original turn deadline has enough time; a failed AI call never causes a vehicle command to be replayed.

Laptop policy decides whether a stage may use cloud and which subset of transcript/history/evidence may leave the device. Jetson accepts requests only from the authorized Laptop service on the intended network; use service authentication and protect the connection when the LAN is not trusted. API keys for cloud providers stay on Laptop. Jetson has no MQTT credentials or vehicle command endpoint.

## 43.6 Migration and acceptance

1. Keep the current Browser `/api/v1` contract and Laptop LangGraph/gateway. Introduce typed `STT`, `LLM` and `TTS` ports around the existing adapters.
2. Deploy the inference-only Jetson service and add remote adapters on Laptop. Verify capability readiness and structured errors with a fake backend before enabling routing.
3. Add a Laptop router using policy, readiness, queue/load and remaining deadline. Record selected backend, attempt and fallback reason in the turn trace.
4. Fault-test Jetson disconnect/reconnect, model loading, queue full, stream cancellation, cloud denied by policy and Internet loss. Verify that handbook retrieval and cited rules/extractive answers still work with both Jetson and Internet disconnected, `session_id`/`turn_id` survive, and no vehicle command executes twice or reports success without read-back.

Measure end-to-end and per-stage p50/p95 latency, first usable audio, model memory, failure rate and fallback outcomes on the actual Jetson profile before setting production timeout/concurrency values. The standalone Nano setup that runs the entire API is documented as a separate profile, not as this Laptop + Jetson topology.
