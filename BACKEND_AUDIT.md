# Backend Audit

## Mitigations

| Area | Risk found | Mitigation |
| --- | --- | --- |
| Gemini schemas | Nullable discovery fields could generate unsupported `anyOf` schemas. | Structured discovery fields use primitive defaults; the Gemini client also strips unsupported wire-schema keywords while retaining local validation. |
| Stored requirements | Malformed JSON could abort execution-draft generation. | Draft parsing now treats invalid requirement collections as empty and records a warning. |
| Document vault | Existing rows may contain missing paths, filenames, or IDs. | Vault matching filters invalid IDs; signed URLs are generated only for usable storage paths; unusable matches require verification. |
| Sensitive data | Query and form values could reach logs or external model prompts unredacted. | Aadhaar-like and account-number digit sequences are redacted before intent/discovery prompts and pipeline logs; form payload responses remain masked. |
| Transactions | Route exceptions could leave sessions in a failed transaction state. | The database dependency rolls back on exceptions and always closes the session. |
| Hardcoded execution fields | Empty service metadata triggered service-name heuristics and default geography. | Execution fields now come only from stored service metadata, the profile, or the request. |
| Execution lifecycle | No persisted asynchronous execution task existed. | `ExecutionTask` stores manifest, payload, state, progress, blockers, errors, evidence, and tracking numbers. |

## Latency behavior

Conversational intent is evaluated before discovery, so greetings do not perform discovery or persistence. Lexical eligibility checks remain first; semantic education equivalence is only used after lexical matching fails. Intent must precede discovery because discovery needs the extracted service category/name; speculative concurrent discovery would risk incorrect scheme retrieval.

## Swagger verification

Start the backend from `gov-agent-backend` with the configured environment:

```powershell
.\venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

Open `/docs` and authorize with the existing bearer-token flow.

1. `POST /api/execution/draft` with a stored `service_id`; inspect `execution_id`, missing fields/documents, masked `form_payload`, and `ready_for_dispatch`.
2. `POST /api/execution/dispatch` with that `execution_id` and `citizen_approved: true`; a ready task returns `202`.
3. `GET /api/execution/status/{execution_id}` to inspect state, progress, portal URL, and blockers.
4. `POST /api/execution/callback/{execution_id}` with a valid lifecycle state and progress to simulate the worker.
5. `POST /api/execution/intervene/{execution_id}` after a callback sets `WAITING_FOR_CITIZEN_INTERVENTION`, with a confirmed intervention payload.
6. `POST /api/agent/assist` with a conversational or actionable query; malformed JSON is rejected by FastAPI validation with a structured `422`, before business logic executes.
