# Requirements: Member bank access

## Why

A company deployment of Hindsight serves people, not only client applications. A person should reach the banks they were given with their own company identity, so nobody hands out or rotates a key per person. A provisioning system outside the charm decides who may use which bank, and needs an API to create banks and grant and withdraw them, without being able to read anyone's memories.

## Stories

- **US-1 (P1)**: A member of a team needs to store and recall memories in the banks granted to their email by signing in as themselves, so nobody holds a key on their behalf. Motivates FR-001, FR-002, FR-003, FR-004, FR-005.
- **US-2 (P1)**: A provisioning system needs to create a bank and grant it to people by email, and withdraw a person's access, so access follows team membership without an operator. Motivates FR-006, FR-007, FR-008, FR-009, FR-010.
- **US-3 (P1)**: An operator needs a member to be unable to reach any bank that was not granted to them, or anything that spans banks, so a shared deployment keeps each team's memories apart. Motivates FR-003, FR-004, FR-011.
- **US-4 (P2)**: An operator needs to turn member access on and off with configuration, and see a clear message for a wrong configuration, so a mistake never leaves the deployment more open than intended. Motivates FR-012, FR-013, FR-014, FR-015, FR-016.

## Requirements

- **FR-001**: WHERE member access is configured, THE SYSTEM SHALL accept as a member any request whose bearer token is a JSON Web Token signed with a key the configured issuer publishes, issued by that issuer, for the configured audience, and not expired, and SHALL take the member's identity from its `email` claim, compared without regard to case.
- **FR-002**: IF a token has no `email` claim, or its `email_verified` claim is false, THEN THE SYSTEM SHALL refuse the request as unauthenticated.
- **FR-003**: WHEN a member reads from, recalls from, reflects on, stores in or changes a bank, including its memories, configuration, mission and disposition, THE SYSTEM SHALL allow it only if the bank is granted to the member's email.
- **FR-004**: WHEN a member deletes a bank, uses a bank not granted to them, including creating it, or requests an operation that is not specific to one bank, THE SYSTEM SHALL refuse it, as it refuses a client key's request that is not on one bank (001 FR-007, FR-008).
- **FR-005**: WHEN a member lists banks, THE SYSTEM SHALL return only the banks granted to the member's email, with the total and paging computed over those banks.
- **FR-006**: WHEN a member uses the MCP endpoint of a bank granted to them, THE SYSTEM SHALL allow the bank's read and write tools, and SHALL refuse tools that span banks and every tool on a bank not granted to them.
- **FR-007**: THE SYSTEM SHALL offer a grants API, authenticated by the grants key or the admin key, with these operations:
  - `PUT /admin/banks/{bank}`: create the bank if it does not exist
  - `GET /admin/banks/{bank}/grants`: list the emails the bank is granted to, as `{"grants": [...]}`, or 404 if the bank does not exist
  - `PUT /admin/banks/{bank}/grants/{email}`: grant the bank to the email, or 404 if the bank does not exist
  - `DELETE /admin/banks/{bank}/grants/{email}`: withdraw the grant, or 404 if there is none
- **FR-008**: THE SYSTEM SHALL make each grants API operation idempotent: repeating a create or a grant changes nothing and succeeds.
- **FR-009**: THE SYSTEM SHALL let the grants key reach the grants API and nothing else.
- **FR-010**: WHEN a grant is given or withdrawn, THE SYSTEM SHALL apply it to the member's next request on every unit.
- **FR-011**: THE SYSTEM SHALL keep grants in Hindsight's database, outside the schema Hindsight keeps banks in, so they are shared by every unit and kept across restarts, and SHALL require no change to the PostgreSQL deployment.
- **FR-012**: THE SYSTEM SHALL read the issuer and audience from the `member-issuer` and `member-audience` options, and the grants key from a Juju secret named by the `grants-key` option.
- **FR-013**: IF `member-issuer` is set and `member-audience` is not, the issuer is not an `https` URL, no admin key is set, or the grants key is missing or equal to the admin key or a client key, THEN THE SYSTEM SHALL block the unit with a message naming the problem and serve no API.
- **FR-014**: WHEN `member-issuer` is cleared, THE SYSTEM SHALL refuse every member token and the grants API, and SHALL keep the grants for when member access is turned on again.
- **FR-015**: IF the issuer's keys cannot be fetched, THEN THE SYSTEM SHALL refuse member requests with a service-unavailable status, and SHALL keep serving the admin key and client keys.
- **FR-016**: WHEN THE SYSTEM refuses a member's request, THE SYSTEM SHALL log the member's email, the method, the path and the reason, and no token.

## Key Entities

- **Member**: a person identified by the verified `email` claim of a token from the configured issuer.
- **Grant**: a bank and an email given access to it, with the time it was given.
- **Grants key**: a key that reaches only the grants API, held by the provisioning system.

## Success Criteria

- **SC-001**: 0 refused member requests reach Hindsight or return memories from a bank not granted to the member, in the unit suite.
- **SC-002**: Each member lists exactly the banks granted to their email in 100% of the listing checks of the test suites.
- **SC-003**: A grant or a withdrawal changes the member's next request in 100% of the checks, on every unit of a multi-unit deployment.
- **SC-004**: The grants key is refused on 100% of the checks of routes outside the grants API.

## Edge Cases

- Emails are compared without regard to case, and the grants API reports them in lowercase.
- A member with no grants authenticates, lists no banks and is refused on every bank.
- An expired token, a token from another issuer, a token for another audience and a token signed with an unknown key are each refused as unauthenticated.
- A token signed with a key the issuer added after the gateway last fetched its keys is accepted once the gateway fetches them again.
- A bearer value that equals the admin key, a client key or the grants key is identified as that key before it is read as a token.
- A grant to a bank that the admin key later deletes stays until it is withdrawn, and applies again if the bank is created again.
- An email in the grants API path that is not a single address is refused with 400.

## Assumptions

- The company identity provider publishes OpenID Connect discovery at `<issuer>/.well-known/openid-configuration`, with a `jwks_uri`, and signs its tokens with an asymmetric algorithm.
- Members' agents send their token as `Authorization: Bearer <token>`, as client keys are sent, and obtain a new one before it expires.
- The `database` relation's user may create a schema in the database it is given, and the embedded database's user is its owner.
- In Hindsight 0.9.2, `PUT /v1/default/banks/{bank}` creates a bank and `GET /v1/default/banks/{bank}/profile` returns 404 for a bank that does not exist.

## Out of Scope

- Read-only grants. They would need a permission on each grant and the provisioning system to send it.
- Grants to groups of people. They would need the identity provider's group claims and a way to name a group in a grant.
- A live demonstration in the integration and spread suites. It would need a test identity provider, served over https with a certificate the gateway trusts, deployed beside the charm.
- Signing in to the Control Plane as a member. It would need per-user sign-in in the Control Plane.
