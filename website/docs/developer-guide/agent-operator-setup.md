# Native operator setup

Normal dashboard sign-in authenticates an account; it does not authorize recipient-policy administration.
The initial server owner approves a device through the existing same-user native owner channel.
The device then administers exact profiles in the app without receiving provider credentials.

The owner runs `hermes operator approve CODE` on the existing Hermes host and reviews the
requesting account, registered app instance and exact profile list before typing `approve`.
`hermes operator list`, `deny CODE` and `revoke GRANT_ID` provide local inspection and revocation.
This initial same-user ownership ceremony is separate from ordinary policy editing inside Agent.

## Pairing protocol (version 1)

All paths below start with `/api/agent/v1/operator`. Responses use `Cache-Control: no-store`.
Creation, polling and redemption require a verified interactive dashboard account. Approval and denial
require the existing local `native-http` grant with exactly `http:owner` authority; client claims never suffice.

- `POST /pairings`: closed JSON `{device_id, profiles, code_challenge, code_challenge_method:"S256"}`.
  Device ID is a canonical UUID. Profiles are unique exact names (1–32), and the challenge is unpadded
  base64url SHA256 (43 characters). Returns a pairing receipt.
- `GET /pairings/{pairing_id}`: the originating authenticated account or local owner can inspect it.
- `POST /pairings/{pairing_id}/approve`: local owner supplies `{profiles:[the exact requested profiles]}`.
- `POST /pairings/{pairing_id}/deny`: local owner supplies `{}`.
- `POST /pairings/{pairing_id}/redeem`: originating account supplies `{device_id, code_verifier}`.
  PKCE verifier follows RFC7636 (43–128 unreserved characters). A successful redemption consumes the
  approved pairing atomically and returns an opaque grant exactly once.
- `GET /status?profile=EXACT`: authenticated readiness receipt with `schema_version`, `installation_id`,
  `account_id`, `profile`, `pairing_supported:true`, and `can_manage_policy`.
- `GET /grants/current`: requires the grant and its originating authenticated account; returns its
  nonsecret principal receipt.
- `DELETE /grants/current`: revokes that grant, returning `{ok:true,schema_version:1,revoked:true,grant_id}`.
- `POST /grants/current/renew`: an explicit in-app action sends `{device_id,operation_id}` with
  the existing valid grant. It atomically rotates to the same scope for thirty days; an exact
  lost-response replay using the same account/device/operation and prior bearer recovers the same
  receipt for two minutes. Other old-bearer operations remain refused. Expired/revoked authority
  cannot initiate renewal; revoked replacement grants cannot be recovered.
- `DELETE /grants/{grant_id}`: the local native owner can revoke a device grant.

A pairing receipt contains `schema_version:1`, `installation_id`, `pairing_id`, `account_id`,
`device_id`, `profiles`, `capabilities`, `verification_code`, `expires_at` (Unix seconds), and `status`
(`pending`, `approved`, `denied`, `redeemed`, or `expired`). Pairings expire after ten minutes.
Redemption returns `schema_version:1`, `installation_id`, `grant_id`, `account_id`, `device_id`,
`profiles`, `capabilities`, `expires_at`, and `operator_grant`. Grants expire after thirty days and
are revocable immediately. Only token hashes are persisted.

The grant is sent in `X-Hermes-Operator-Grant` alongside normal authenticated requests, never in a URL.
Its fixed capability set is `recipient-policy:read` and `recipient-policy:write`. It grants no model,
provider, shell, filesystem, runtime update, or AI-sharing consent authority. Installation, account,
device and exact profile membership remain bound. Another account cannot use the grant after sign-in changes.
`device_id` identifies the registered app instance at pairing and redemption; this is standard PKCE
bearer delegation, not per-request cryptographic proof of possession. Store the bearer in device-only
Keychain storage, use TLS, and renew explicitly while current authority remains valid.

## Supported plugin interface

`from hermes_cli.plugins import operator_authorization` and `ctx.operator_authorization` expose
`(request, *, profile, capability)`. They return `None` on absent, invalid, expired, revoked, or
wrong-scope grants, or an immutable mapping with `schema_version:1`, `purpose:"recipient-policy"`,
`installation_id`, `account_id`, `device_id`, `profiles` (tuple), `profile`, `capabilities` (frozenset),
`grant_id`, and `expires_at`. Storage failure raises HTTP503 `operator_authority_unavailable`.
Call before reading a protected body and again immediately before a write after intervening awaits.
Verified local native-owner requests use the same immutable receipt with `source:"local-native-owner"`
and nullable `grant_id`, `device_id`, and `expires_at`; their exact authenticated native profile home
must match the selected and bound profile too.

Hosted dashboard APIs receive only a verified principal over the existing private IPC channel;
the raw grant header is stripped. The helper rechecks persisted revocation and expiry when used.
Whole-policy local-owner operations remain unchanged. Profile-scoped companion operations must merge
one exact profile atomically using an expected revision, never replace another profile's declaration.

Operator declarations and user permission are separate. Saving a declaration never authorizes an
AI send, automatic personal-context sharing or Health access; incomplete/opaque declarations remain blocked.

## Optional client capabilities

An owning extension can register `ctx.register_client_capability(feature, async_probe, paths=(...))` during its
normal scoped registration lifecycle. Supported names are `groups`, `usage_upload`, `life_context`,
`remote_push` and `live_activities`. A plugin name, enabled setting or route's presence is not proof.
The callback receives a frozen context with `installation_id`, exact authenticated `account_id`,
`profile` and the request, and must verify its own authenticated backend's versioned contract.
`paths` lists at most four unique query-free read paths under that owning plugin's API prefix;
hosted proofs require this explicit contract-path allowlist.

`await context.read_json("/api/plugins/EXTENSION/status")` makes a read-only same-app ASGI request
with the exact profile selector, existing request authentication and normal route authorization.
It uses no external socket and accepts no arbitrary host, method, query or traversing path.
After checking its owning route's closed identity-matching schema, the extension returns
`context.receipt(feature, True)`. The receipt has exactly `schema_version:1`, `contract_version:1`,
`feature`, `installation_id`, `account_id`, `profile` and boolean `supported`.

Native capability responses advertise a feature only for that exact successful receipt. Absence,
unsupported registration, timeout, wrong owner/profile/version/schema, denial or backend failure
keeps the feature false. Probes are asynchronous and bounded to two seconds each; their registration
is profile scoped and removed with the owning plugin. These receipts advertise protocol support;
they never grant user consent, enable Health, enroll a device or perform a write.

With plugin isolation enabled, the callback receives a restricted RPC facade rather than a serialized
parent request. A random invocation permit binds the installation, exact interactive account, selected
profile, feature and registered paths for two seconds. Every allowed path can be read once; finished,
cancelled or expired proofs cannot read again. The parent executes each authorized GET through the
actual same-app router and rechecks the account and profile before and after it.

Owning hosted APIs receive a fresh frozen projection containing only the verified session's provider,
issuer, user, organization and expiry over private IPC. The child verifies the installation, exact
authenticated account and target profile home again. No raw bearer, cookie, operator grant, native
ticket, access token or refresh token is forwarded. HTTP headers, query values and JSON cannot supply
this session projection. Ordinary hosted admission alone does not establish a policy operator grant.
