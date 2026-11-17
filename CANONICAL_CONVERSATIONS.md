# Native conversation protocol

The `session-conversations-v1` capability extends the existing authenticated Hermes session owner with ongoing conversations, durable message references and independent tasks. It uses the same ticketed JSON-RPC WebSocket, authority, FIFO admission ledger and transcript writer. It adds no service, authentication scheme, model tool or provider default.

Clients require both authenticated HTTP `GET /api/agent/v1/capabilities?profile=P` with `core_chat_protocol: canonical-session-authority` and a fresh canonical WebSocket `runtime.describe` containing `session-conversations-v1`. A version number or a metadata plugin's presence does not establish protocol support. The standalone legacy compatibility protocol remains a separate contract.

## Profile and session identity

Every create/resume snapshot's `info.profile_id`, live/replayed event's `profile_id`, active-list row and server-request parameters identify the owning profile. Session IDs are unique only within that profile. A host-scoped socket routes methods using an explicit `profile` selector; a profile-bound grant cannot name a sibling. Replies, approvals and Stop stay bound to their original profile, admission and execution generation.

Canonical `session.create`, `session.resume`, `session.interrupt`, `approval.respond`, `clarify.respond` and `session.events.since` keep their native contracts. Clients must handle native revisions, generation fences, queued/started/unknown/terminal admissions and replay gaps; the legacy client frame shapes are insufficient.

## Replies and attachments

`prompt.submit` accepts optional `conversation_session_id`, `reply_to` and `conversation_attachments`. It still requires a durable `submission_id` (use the same UUID for retries) and returns the standard FIFO admission receipt. A queued receipt proves admission, not completed inference or an already-persisted user row.

A `reply_to` contains exactly `session_id`, numeric `row_id` and immutable `message_uid`. A new admission validates the reference against the selected profile and current compression conversation in the same transaction that commits its FIFO row. The server stores a bounded source snapshot; the client cannot supply quoted source text. Reset, branch, delegate and independent-task boundaries do not join compression conversations.

The executing user row receives `display_metadata.agent_operation_id` and, for a reply, `display_metadata.agent_reply_to`. The quoted reference is added only to this new model turn; the visible user text and past cached context are preserved. The in-process and managed-worker adapters carry the same reference. Exact retries return the original admission and snapshot even if its source message has since been deleted; changed input under the same request ID is refused.

Each `conversation_attachments` descriptor contains `path`, `kind` (`image`, `file` or `pdf`), `name`, `mime_type` and exact byte `size`. At most 16 files of 25 MiB each are accepted. Existing managed-file, profile and sensitive-path restrictions apply. The owner privately verifies the complete batch before retaining immutable native-input references; rejected preparation admits nothing. A retry can reconcile an expired upload against the retained manifest. Pending inputs reverify retained bytes, while terminal inputs use their original digest evidence. This field cannot be mixed with the older image-only `attachments` field in one submission.

## Ongoing conversations and tasks

`session.ongoing_open` accepts `profile` and optional `source` (`ios` or `desktop`). Its response has `profile`, `stored_session_id`, `resolved_session_id`, a canonical `session` snapshot and `created`. The native Bot Chat target owner selects, creates and restores the ongoing conversation through its existing durable creation receipts and exact identity. The app follows deliberate native handoff, reset and replacement operations. A cached roster mapping is refreshed only from that owner; a numbered topic such as `Bot Chat #2` cannot take over the entry point. Deleting the destination retires its cached mapping.

`session.task_create` accepts `profile`, UUID `operation_id`, exact stored `origin` reference, `title` (1–120 characters) and optional `source`. It returns a `thread`, canonical `session` snapshot, `resolved_session_id` and `replayed`. A task is a new independent native session with its own FIFO, approvals, Stop controls and draft. Creation commits the native route, bounded hidden source excerpt, thread metadata and retry identity together. Its source association is metadata, not a transcript parent edge; deleting the source does not cascade into the task.

Thread JSON contains `thread_id`, `profile`, `title`, `parent_session_id`, `origin` (the server's source anchor) and Unix-seconds `created_at`. Exact retries preserve this association. Deleting a task retires its copied metadata and native creation receipt; a retry cannot recreate deleted work.

## Authenticated reads

These projections use the existing authenticated HTTP actor and owning profile authority, with `Cache-Control: no-store`:

- `GET /api/agent/v1/capabilities?profile=P`
- `GET /api/agent/v1/messages/resolve?profile=P&session_id=S&row_id=N`
- `GET /api/agent/v1/sessions/S/links?profile=P&limit=100&after=N`
- `GET /api/agent/v1/threads?profile=P&origin_session_id=S&limit=100&after=CHILD_ID`
- `GET /api/agent/v1/threads/CHILD_ID?profile=P`
- `GET /api/agent/v1/roster`

Source anchors contain `profile`, exact physical `session_id`, `row_id`, `message_uid`, `role` and bounded `snapshot` (`text`, Unix-seconds `timestamp`, `truncated`). Read replies echo the requested view as `conversation_session_id`, while source anchors preserve their physical IDs. Link pagination uses physical row IDs; thread pagination uses child IDs. A `null` next cursor ends traversal. Link projection reuses native compression-copy deduplication and omits rewound replies.

The typed wire is published through the generated canonical OpenRPC and TypeScript contracts. Test the protocol through `scripts/run_tests.sh`; use a temporary Hermes home, real owner/SQLite admission and synthetic inference, with no user credentials or production writes.
