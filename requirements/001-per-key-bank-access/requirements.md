# Requirements: Per-key bank access

## Why

One Hindsight deployment often serves several client applications, such as agents. With a single API key, every client can read, change and delete every other client's memories. Operators need a key per client, limited to that client's memory banks, without separate database schemas or changes to the managed PostgreSQL.

## Stories

- **US-1 (P1)**: An operator running Hindsight for several client applications needs each client limited to its own banks, so one client cannot read, change or discover another's memories. Motivates FR-001, FR-003, FR-004, FR-005, FR-008, FR-009, FR-016.
- **US-2 (P1)**: An operator needs keys that read some banks without changing them, so an auditor or a shared handbook can be read safely. Motivates FR-004, FR-005, FR-006.
- **US-3 (P1)**: An operator needs the admin key and the Control Plane to keep full access to every bank, so the deployment can still be managed. Motivates FR-002, FR-007, FR-011.
- **US-4 (P2)**: An agent that uses MCP needs the same limits on its bank's MCP endpoint, so MCP does not undo the isolation. Motivates FR-010.
- **US-5 (P2)**: An operator needs a clear message for a wrong configuration and a way to turn the feature off, so a mistake never leaves the deployment more open than intended. Motivates FR-012, FR-013, FR-014, FR-017.
- **US-6 (P3)**: A reviewer needs the feature demonstrated against a live deployment as readable steps, so the behavior can be checked and rerun. Motivates SC-006.

## Requirements

- **FR-001**: WHERE per-key access is configured, THE SYSTEM SHALL accept requests authenticated with the admin key or a configured client key, and refuse every other request to a non-public route as unauthenticated.
- **FR-002**: WHERE per-key access is configured, THE SYSTEM SHALL give the admin key full access to every bank and every operation.
- **FR-003**: WHEN a client key lists banks, THE SYSTEM SHALL return only the banks the key may read, with the total and paging computed over those banks.
- **FR-004**: WHEN a client key reads from, recalls from or reflects on a bank, THE SYSTEM SHALL allow it only if the bank matches the key's read-write or read-only patterns.
- **FR-005**: WHEN a client key writes to, changes, deletes or creates a bank, including its memories, configuration, mission and disposition, THE SYSTEM SHALL allow it only if the bank matches the key's read-write patterns.
- **FR-006**: IF a client key's request is refused because the bank is read-only for that key, THEN THE SYSTEM SHALL say so in the response.
- **FR-007**: WHEN a client key requests an operation that is not specific to one bank, THE SYSTEM SHALL refuse it. These include:
  - metrics
  - chunks and files fetched by ID
  - the multi-bank MCP endpoint
- **FR-008**: WHEN a client key makes a request that THE SYSTEM does not recognise as an operation on one bank, THE SYSTEM SHALL refuse it.
- **FR-009**: IF a request path contains a relative segment, an empty segment or an encoded slash, THEN THE SYSTEM SHALL refuse it before Hindsight receives it.
- **FR-010**: WHEN a client key uses the MCP endpoint of a bank, THE SYSTEM SHALL apply the key's access to that bank, allow read tools on readable banks and write tools only on read-write banks, and refuse tools that span banks.
- **FR-011**: WHERE per-key access is configured, THE SYSTEM SHALL keep the Control Plane working with the admin key's view of every bank.
- **FR-012**: THE SYSTEM SHALL read client keys from a Juju secret named by the `access-keys` option, and each client's read-write and read-only bank patterns from the `bank-access` option.
- **FR-013**: IF `bank-access` is invalid, names a client without a key, gives two clients the same key, gives a client the admin key, or no admin key is set, THEN THE SYSTEM SHALL block the unit with a message naming the problem and serve no API.
- **FR-014**: WHEN `bank-access` is cleared, THE SYSTEM SHALL serve the API directly, accepting only the admin key.
- **FR-015**: THE SYSTEM SHALL keep every bank in Hindsight's existing database and schema, and require no change to the PostgreSQL deployment.
- **FR-016**: WHERE per-key access is configured, THE SYSTEM SHALL keep client requests from reaching Hindsight except through the access checks.
- **FR-017**: WHEN THE SYSTEM refuses a client key's request, THE SYSTEM SHALL log the client's name, the method, the path and the reason, and no key.

## Key Entities

- **Client**: an application with a name, one API key, read-write bank patterns and read-only bank patterns.
- **Admin key**: the existing `tenant-api-key`, with full access.
- **Bank pattern**: an exact bank name or a shell-style pattern, matched case-sensitively against the whole bank name.

## Success Criteria

- **SC-001**: 0 refused cross-tenant requests (reads, recalls, writes, deletes, exports) reach Hindsight or return another client's memories, across the unit, integration and spread suites.
- **SC-002**: Each client key lists exactly the banks its patterns allow in 100% of the listing checks of the test suites.
- **SC-003**: Read-only keys succeed on 100% of read checks and are refused on 100% of write, create and delete checks.
- **SC-004**: An operator adds a client with its banks in 3 Juju commands or fewer.
- **SC-005**: A change to `bank-access` or `access-keys` takes effect within 5 minutes, without redeploying.
- **SC-006**: A spread suite demonstrates stories 1 to 4 in 6 or more separately reported tasks, and runs in CI.

## Edge Cases

- Bank patterns match the whole name, case-sensitively: `a-*` matches neither `A-notes` nor `xa-notes`.
- A client key that matches no pattern authenticates, lists no banks and is refused on every bank.
- A client that creates a bank matching its read-write patterns sees it, and other clients do not.
- An MCP request that batches several tool calls is refused if any call is refused, and an unreadable MCP request is refused.
- A route that Hindsight adds in a later release is refused to client keys until the charm classifies it.
- A key in `access-keys` with no entry in `bank-access` is ignored.

## Assumptions

- Clients send their key as `Authorization: Bearer <key>`, as they do for the admin key.
- Reflect reads memories and stores nothing in the bank. It spends LLM tokens on the operator's provider key.
- The Control Plane is an admin tool and keeps seeing every bank.
- Hindsight 0.9.2 skips its own per-bank checks on several routes, so the access checks do not rely on them.
- In Hindsight 0.9.2, a bank's MCP endpoint takes the bank only from its URL, and its tools take no bank argument.

## Out of Scope

- Isolation in separate PostgreSQL schemas per key, as Hindsight's tenant extensions offer. It would need the charm to create schemas in the managed database.
- Restricting the Control Plane per client. It would need per-user sign-in in the Control Plane.
- Refusal counts as metrics. They would need a metrics endpoint and the COS relations.
