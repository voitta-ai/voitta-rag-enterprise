"""In-app assistant: an LLM chat grounded in the RAG index and sync state.

Layout (see docs/OPERATIONS.md §11 for the operator view):

- ``catalog``      engines, models, effort levels
- ``policy``       deployment switch + defaults (super-admin managed)
- ``identity``     real vs effective identity; who pays, whose data
- ``credentials``  encrypted LLM credentials: storage, resolution, probes
- ``store`` / ``transcript``  append-only conversations in engine-neutral blocks
- ``tools``        the tool registry (RAG retrieval + sync overview)
- ``prompts``      the fixed system prompt
- ``protocol``     events a turn emits (the WebSocket wire format)
- ``engines``      ``anthropic_api`` (own tool loop) / ``claude_subscription``
                   (Claude Agent SDK)
- ``turns``        TurnRunner: one active turn per conversation, persistence,
                   cancellation, concurrency limits
"""
