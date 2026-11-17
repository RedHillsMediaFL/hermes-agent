# Native client attribution v1

This compatibility branch extends Hermes v0.23.2's standalone, authenticated legacy dashboard
transport. It adds durable reply attribution, independent tasks and an explicit per-profile
ongoing conversation. It does not change model/provider defaults, install a second chat engine,
enable telemetry, or claim compatibility with the canonical session-authority transport.

Clients first read `GET /api/agent/v1/capabilities?profile=default`, verify the serving profile,
then independently verify a fresh authenticated WebSocket `gateway.ready` frame. All new chat
features are false on the canonical transport; a capability document is not a transport handshake.
Existing dashboard authentication, native PKCE bearer tokens and single-use WebSocket tickets
remain the authentication boundary. Hermes remains a single-owner application: different dashboard
usernames are not separate tenants.

## Protocol

Source references contain exactly `session_id`, numeric `row_id` and immutable `message_uid`.
The server resolves them against the selected profile and verified owner, including the stock
local-history authorization rule. A row/UID mismatch, foreign owner, routed unowned row, missing
source or wrong profile is refused. The server produces bounded reference snapshots; clients
cannot submit their own snapshot text or identity.

- `session.submit_attributed`: `profile`, runtime `session_id`, UUID `operation_id`, `text`,
  `reply_to` and optional `attachments`. The user row, exact reply reference and operation receipt
  commit in one SessionDB writer transaction. A busy conversation returns RPC error `4091`,
  `code: session_busy`, `accepted: false`, `retryable: true`, without queueing or steering.
- `session.task_create`: `profile`, UUID `operation_id`, `origin`, `title`, and optional `source`
  (`ios` or `desktop`, default `ios`). A new independent session, bounded hidden reference excerpt,
  thread metadata and retry receipt commit together. The parent transcript is not copied wholesale.
- `session.ongoing_open`: `profile` and optional `source` (`ios` or `desktop`, default `ios`).
  A durable per-profile map ensures one ongoing session without title or recency inference.
  Compression is projected through `ongoing_resolved_session_id` in the roster.

Exact retries replay the original association with `replayed: true`; a repeated operation ID with
a changed payload is `operation_conflict`. An accepted retry has `status: accepted` and
`disposition: already_accepted`. It never starts inference again or claims a crashed turn resumed.
Deleting an accepted target leaves a scrubbed operation tombstone; its retry returns
`operation_target_deleted` instead of recreating deleted work. A retained reply/task quotation is
part of the destination transcript, and deleting the source does not delete that destination.

The selected reply excerpt is added to the current user turn as quoted reference data. It never
changes the system prompt or earlier cached conversation context. The visible user row keeps the
user's own text and durable display metadata. Inline and isolated compute workers adopt the exact
admitted row rather than writing a second user message.

## Read routes

All routes use existing authenticated HTTP access and return `Cache-Control: no-store`.
Pass the selected `profile` explicitly.

- `GET /api/agent/v1/capabilities?profile=P`
- `GET /api/agent/v1/messages/resolve?profile=P&session_id=S&row_id=N`
- `GET /api/agent/v1/sessions/S/links?profile=P&limit=100&after=N`
- `GET /api/agent/v1/threads?profile=P&origin_session_id=S&limit=100&after=CHILD_ID`
- `GET /api/agent/v1/threads/CHILD_ID?profile=P`
- `GET /api/agent/v1/roster`

Timestamps are JSON numbers in Unix seconds. Message-link pagination uses numeric physical row
IDs; thread pagination uses child session IDs. A `null` `next_cursor` ends traversal.

## Attachments

Each descriptor contains `path`, `kind` (`image`, `file` or `pdf`), `name`, `mime_type` and exact
byte `size`. At most 16 files of 25 MiB each are accepted. Existing managed-file root restrictions
and sensitive-file guards apply to the authenticated WebSocket owner just as they do to HTTP.
Explicit input files are bounded copies into one private operation-owned staging directory;
descriptor size, regular-file identity and mutation checks run before acceptance. Failed preparation
removes the entire operation directory. Pending shared image buffers are neither consumed nor
modified. A retry replays before reopening a source file, so uploads may expire after acceptance.

PDF rendering requires Poppler's `pdftoppm` and is bounded to the first 25 pages at 1600 pixels.
Missing converters and invalid/failed rendering refuse the entire send before transcript admission.
This is attachment preparation, not an assertion of provider vision support.

## Verification

Run the canonical `scripts/run_tests.sh` with an isolated Hermes test environment. Focused tests
cover transaction rollback, concurrent retry, restart replay, deletion tombstones, owner/profile
fences, task/ongoing identity, native PKCE plus real authenticated HTTP/WebSocket dispatch, busy
refusal, managed attachment restrictions and PDF preparation failure. The HTTP/WS admission test
intentionally does not invoke a model provider; it is not live-inference acceptance.
