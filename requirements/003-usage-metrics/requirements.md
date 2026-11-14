# Requirements: Usage metrics

## Why

A shared deployment of Hindsight serves several client applications and teams, and its LLM calls cost money. An operator needs to see how much each client calls Hindsight, and what each bank costs in LLM tokens, so usage can be charged to the team that owns it and a client that misbehaves can be found. An operator also needs to follow a client's request into Hindsight, under the trace the client started, and to charge the LLM calls a client's retains cause to that client. The metrics and traces belong in the Canonical Observability Stack (COS) beside the rest of the model's.

## Stories

- **US-1 (P1)**: An operator needs the number of calls each client key, member and admin makes, by bank, operation and result, so the load on a shared deployment can be traced to whoever caused it. Motivates FR-001, FR-002, FR-003, FR-004.
- **US-2 (P1)**: An operator needs the LLM calls, tokens and cost of each bank, so LLM spend can be charged to the bank's owner. Motivates FR-005, FR-006, FR-007, FR-008, FR-009.
- **US-3 (P1)**: An operator needs the metrics scraped by COS when the charm is related to it, so no scrape configuration is written by hand. Motivates FR-010, FR-011, FR-012.
- **US-4 (P2)**: An operator needs a span for each request the gateway answers, in the trace the client started, with the caller it was answered for, so a client's requests can be followed in COS and into Hindsight. Motivates FR-013, FR-014, FR-015, FR-016, FR-017.
- **US-5 (P2)**: An operator needs the LLM calls, tokens and cost of the retains each client sends, and those calls in the client's trace, so a client's LLM spend can be charged to it even though Hindsight 0.9.2 does not continue the trace it receives. Motivates FR-008, FR-018, FR-019, FR-020, FR-021.

## Requirements

- **FR-001**: WHILE the gateway runs, THE SYSTEM SHALL count each request it answers by caller, bank, operation and HTTP status class, and SHALL serve the counts in the Prometheus text format at `/metrics` on port 9464.
- **FR-002**: THE SYSTEM SHALL name the caller of a counted request by the client's name for a client key, and as `admin`, `grants`, `member` or `unauthenticated` otherwise, and SHALL NOT put a key, a token or an email in a metric.
- **FR-003**: THE SYSTEM SHALL label a request with its bank only when the caller was authenticated and the request was allowed, so a refused or unauthenticated request cannot add a bank name to the metrics.
- **FR-004**: THE SYSTEM SHALL name the operation of a request as one of `retain`, `recall`, `reflect`, `read`, `write`, `delete-bank`, `list-banks`, `mcp`, `grants`, `admin` or `invalid`, and SHALL NOT count requests to public routes such as `/health`.
- **FR-005**: WHILE the gateway runs on the leader unit, THE SYSTEM SHALL add the calls and the input, output and cached tokens of every LLM call Hindsight records to a running total per bank, client of FR-019, Hindsight operation, provider and model, kept in the `hindsight_gateway` schema of Hindsight's database, and SHALL count each recorded call once.
- **FR-006**: THE SYSTEM SHALL keep the totals of FR-005 when Hindsight deletes the records they were added from, when a unit restarts, and when leadership moves to another unit.
- **FR-007**: THE SYSTEM SHALL serve the totals of FR-005 at the leader's `/metrics`, and a cost in US dollars per total, priced with the LiteLLM price list bundled in the Hindsight image. A model the price list does not name gets no cost.
- **FR-008**: WHILE the gateway runs, THE SYSTEM SHALL have Hindsight record each LLM call's tokens, and SHALL keep at most 256 characters of each call's prompt, output and metadata in the record, unless the operator sets Hindsight's tracing settings in `extra-env` or `env-secret`.
- **FR-009**: IF Hindsight's database cannot be read, THEN THE SYSTEM SHALL report `hindsight_gateway_llm_usage_up` as 0, keep serving the request counts, and keep the calls Hindsight recorded meanwhile for the next successful read.
- **FR-010**: THE SYSTEM SHALL serve Hindsight's own metrics on the same `/metrics` after the gateway's.
- **FR-011**: WHEN the charm is related to Prometheus over `metrics-endpoint`, THE SYSTEM SHALL publish a scrape job for port 9464 on every unit while `bank-access` or `member-issuer` is set, and no scrape job otherwise.
- **FR-012**: THE SYSTEM SHALL NOT serve the metrics port through the `ingress` relation.
- **FR-013**: WHEN the gateway receives a request with a valid W3C `traceparent` header, THE SYSTEM SHALL continue that trace, and SHALL start a new trace otherwise.
- **FR-014**: THE SYSTEM SHALL record a span for each request it counts by FR-001, with the caller, bank, operation and HTTP status of FR-002 to FR-004, and SHALL NOT put a key, a token, an email, a request path or a body in a span.
- **FR-015**: WHEN the gateway calls Hindsight for a request, THE SYSTEM SHALL send a `traceparent` naming the request's span as the parent, in place of the client's, and SHALL NOT add the caller to `baggage`.
- **FR-016**: WHEN the charm is related to a tracing provider over `tracing`, THE SYSTEM SHALL export the gateway's spans to the provider's OTLP HTTP endpoint, and SHALL export none otherwise. FR-013 and FR-015 apply in both cases.
- **FR-017**: THE SYSTEM SHALL NOT turn on Hindsight's own OpenTelemetry trace export, which records prompts and outputs, when the charm is related over `tracing`.
- **FR-018**: WHEN the gateway forwards a retain it allowed, over the HTTP API or an MCP `retain` or `sync_retain` tool, THE SYSTEM SHALL first record each document ID the retain names, with its bank, the caller of FR-002 and the request span's trace and span IDs, in the `hindsight_gateway` schema. A later retain of the same document in the same bank SHALL replace the record, and a record SHALL be deleted 7 days after it was written.
- **FR-019**: WHEN the leader adds an LLM call to the totals of FR-005, THE SYSTEM SHALL name its client as the caller recorded by FR-018 for the call's bank and the document ID in the call's metadata, and as `unattributed` when there is no such record.
- **FR-020**: WHEN the leader attributes an LLM call by FR-019 to a sampled trace, THE SYSTEM SHALL record a span for the call as a child of the retain's span, with the call's start and end times, provider, model, tokens, status and the caller, and SHALL NOT put a prompt, an output or a document ID in the span.
- **FR-021**: THE SYSTEM SHALL forward a retain's body unchanged, SHALL NOT add a document ID to a retain item, and SHALL forward the retain IF the record of FR-018 cannot be written within 2 seconds.

## Key Entities

- **Request count**: requests answered by the gateway, by caller, bank, operation and status class.
- **LLM usage**: the running total of LLM calls and tokens of one bank, client, Hindsight operation, provider and model.
- **Retained document**: a document ID a retain named, with its bank, the caller and the request span, kept for 7 days.
- **Request span**: one request the gateway answered, in the client's trace, with the caller, bank, operation and status.

## Success Criteria

- **SC-001**: In the unit suite, each request of every caller kind is counted once, with the caller, bank, operation and status class it was answered with.
- **SC-002**: In the unit suite, against a real PostgreSQL, the totals match the sum of the recorded LLM calls after Hindsight deletes the records, after two gateways sweep at once, and after a sweep that failed.
- **SC-003**: In the unit suite, the charm publishes a scrape job for port 9464 exactly while `bank-access` or `member-issuer` is set.
- **SC-004**: In the unit suite, the gateway continues a client's trace, starts one when the client sends none or an invalid one, names its own span as the parent of the call to Hindsight, and exports its spans over OTLP HTTP to a stand-in endpoint, with the caller and no key, token or email.
- **SC-005**: In the unit suite, the charm gives the gateway the tracing provider's OTLP HTTP endpoint exactly while the `tracing` relation provides one.
- **SC-006**: In the unit suite, against a real PostgreSQL, the LLM calls of a document retained through the gateway are totalled under the caller that retained it, every other call under `unattributed`, and a sampled retain's trace holds the gateway's span with a child span for each of those calls.

## Edge Cases

- A record with no bank, such as an LLM call outside a bank operation, is added to the total of the bank `""`.
- A record of a failed LLM call counts as a call with the tokens it reports, which may be none.
- Several units run the gateway: each counts its own requests, and only the leader serves the LLM totals, so a sum across units counts each LLM call once.
- A request refused before its caller is known, such as one with an invalid path, is counted with the caller `unauthenticated`.
- A client's `traceparent` with the sampled flag clear: the gateway's span is not exported, and the flag is passed on to Hindsight.
- A request to a public route such as `/health` gets no span, and its trace headers are forwarded unchanged.
- A retain item without a document ID gets one from Hindsight that the gateway never sees, so its LLM calls are `unattributed`.
- A document ID longer than about 230 characters does not fit in the 256 characters of metadata Hindsight keeps, so its LLM calls are `unattributed`.
- Two callers retain the same document of a bank: the calls Hindsight records after the later retain is recorded go to the later caller, including any left over from the earlier retain.
- Reflect, consolidation and mental model refreshes carry no document a caller named, so their calls are `unattributed`.
- An unsampled retain is still attributed, and its LLM calls get no span.

## Assumptions

- Hindsight 0.9.2 records every LLM call, with its bank, provider, model and token counts, in the `llm_requests` table of its schema (`HINDSIGHT_API_DATABASE_SCHEMA`, `public` by default), and deletes records older than `HINDSIGHT_API_LLM_TRACE_RETENTION_DAYS`, one day by default.
- With `HINDSIGHT_API_LLM_TRACE_MAX_CHARS` set to 256, Hindsight 0.9.2 keeps the first 256 characters of the JSON of a call's prompt, output and metadata, with their lengths. The start of a prompt is Hindsight's own instructions. The start of an output can hold facts extracted from a retain, which the same database keeps as memories.
- Hindsight 0.9.2 puts the document ID of each retain extraction call in the record's `metadata.document_id`, when the call is written, and takes the document ID of a retain item as given.
- The input tokens Hindsight records include the cached input tokens.
- The Hindsight image has the LiteLLM price list at `litellm/model_prices_and_context_window_backup.json` in its Python environment.
- The Hindsight image has the OpenTelemetry SDK and its OTLP HTTP exporter, 1.44 or later, in its Python environment.
- The tracing provider's endpoint is reachable with the model's proxy settings, and serves plain HTTP or HTTPS with a certificate the image trusts.

## Out of Scope

- Metrics while the gateway does not run. Hindsight then serves its own `/metrics` with the admin key, and the charm would need a second scrape path.
- Requests per member email. An email in a metric label would reach every reader of the metrics.
- LLM calls of reflect, consolidation and mental models per client. Hindsight 0.9.2 starts a new trace for each operation and ignores the `traceparent` it receives, and these calls name no document, so they cannot be joined to the request that caused them. Once Hindsight continues the trace it receives, the trace ID on each `llm_requests` record joins the call to the gateway's span and its caller. A consolidation run serves several requests, so its cost would still be shared between their callers.
- Retains of files (`/files/retain`). Their document IDs are in a multipart body the gateway does not read.
- Reasoning tokens. Hindsight 0.9.2 does not record them in `llm_requests`.
- Grafana dashboards and alert rules.
