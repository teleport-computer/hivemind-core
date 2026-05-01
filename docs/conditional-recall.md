# Mental Model: Attested Recall Agreements

hivemind-core treats a room as an agreement runtime, not as a shared database.
The owner and participant do not need to trust each other, but they both need
to trust the signed room manifest and the measured CVM that enforces it.

## The Core Shape

```text
Owner                                      Participant
-----                                      -----------
private data                              question
scope agent                               optional query agent
room rules                                invite token
    |                                          |
    +-------------------+----------------------+
                        |
                 signed room manifest
                        |
                 attested dstack CVM
                        |
      +-----------------+-----------------+
      |                                   |
 scope stage                         query stage
 full-read tools                     scoped tools only
 simulation tools                    no raw database access
 query-source inspection             no arbitrary source inspection
      |                                   |
      +-----------> scope_fn ------------+
                        |
               signed room output
```

The owner creates the room and signs the manifest. The participant decides
whether to enter after inspecting the manifest, visible agent source or
digests, and live CVM attestation.

## The Room Manifest

The manifest is the contract both parties verify. It commits to:

- room rules and policy text;
- the scope agent identity and visibility mode;
- whether the query agent is fixed by the owner or uploaded by the
  participant;
- query-agent and mediator-agent visibility;
- output visibility, LLM egress, and artifact egress;
- deployment trust policy and accepted compose hashes;
- the owner public key.

The invite token authorizes access, but the signed manifest is the thing a
client verifies before presenting private material.

Query-agent visibility also controls prompt visibility. In `inspectable` rooms,
past run prompts are stored in run history. In `sealed` rooms, prompt plaintext
is not stored; the signed run attestation keeps only a prompt hash.

## Scope Agent And Query Agent

The scope agent still has the room's superpowers. Those powers are deliberately
scope-only:

- inspect visible query-agent source;
- see query-agent file paths and digests even when bytes are sealed;
- read the room's decrypted data snapshot during the scope stage;
- run read-only SQL over user tables;
- simulate the room's query agent against candidate scope functions;
- batch-test or synthetically verify candidate scope functions.

The query agent does not inherit those powers. It receives the prompt and the
final `scope_fn` source, then every data request goes through host tools that
apply `scope_fn` before returning rows.

```text
scope agent
  -> reads data and visible query-agent context
  -> optionally simulates the query agent
  -> returns: def scope_fn(sql, params, rows): ...

query agent
  -> asks tools for SQL or room-vault data
  -> receives only rows allowed by scope_fn
  -> produces raw answer and optional artifacts

mediator, pinned by the room when configured
  -> sees raw answer and policy
  -> has no data tools
  -> produces final output
```

So the privacy boundary is not "the query agent promises to behave." The
boundary is that the query agent can only obtain data through tools wrapped by
the scope agent's compiled function.

## Data Flow

```text
POST /v1/rooms
  -> owner signs manifest
  -> server mints hmroom invite
  -> room DEK is created and wrapped to owner + invite

POST /v1/rooms/{room_id}/data
  -> owner opens room key
  -> plaintext is encrypted under room DEK
  -> ciphertext is stored in Postgres

POST /v1/rooms/{room_id}/runs
  -> caller opens room key with owner or invite bearer
  -> room vault is decrypted into CVM memory
  -> scope agent builds scope_fn
  -> query agent runs with scoped tools
  -> output is signed with the CVM run signer
```

Room data is encrypted before it reaches Postgres. The database stores
ciphertext, metadata, and key wraps. Plaintext room data exists only in CVM
memory while a valid run is being prepared or executed.

## Restart And Update Behavior

The room key cache lives only in process memory. After a service restart or
CVM update:

- encrypted room data remains in Postgres;
- sealed query-agent source remains encrypted;
- invite tokens minted by the owner carry encrypted DEK wraps, so an invite
  holder can reopen the room after a restart without a separate owner request;
- that participant should verify live attestation and the room trust policy
  before presenting the invite token to the new process.

This is why attestation is part of the access flow instead of a separate
status page.

## Trust Policy

The room trust mode says which CVM measurements the participant accepts:

- `operator_updates`: trust the operator governance path for upgrades.
- `pinned`: trust only the exact compose hashes in the manifest.
- `owner_approved`: trust the owner-maintained allowlist for this room.

Changing room trust re-signs the same room id. Existing invite links keep
working because clients verify the new room envelope against the same owner
public key.

For non-local services, the CLI also requires live CVM proof by default: DCAP
quote verification must recover the dstack compose hash, and the observed TLS
certificate must match the quote's REPORT_DATA v2 binding. `hivemind trust
attest --reproduce` then checks that the live `app_compose` hash, registered
source pointer, and deterministic deploy render hints describe the same compose
YAML that is running in the enclave.

## What This Is Not

It is not a general shared database. Query tokens cannot list room data
directly.

It is not a guarantee that no one learns anything. The point is to enforce the
agreement about what can be learned and exported.

It is not a replacement for careful room rules. If a participant accepts a bad
manifest, the CVM can faithfully enforce the wrong agreement.
