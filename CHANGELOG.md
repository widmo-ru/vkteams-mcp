# Changelog

## 0.2.0 (2026-11-16)

The server keeps nothing: any number of replicas sharing one secret serve the same clients.

- The person's VK Teams session travels sealed (AES-GCM) inside their OAuth tokens, which the client
  refreshes itself by the standard. The client's registration is its `client_id`; the authorization code,
  the sign-in code step and file links carry what they need. No database, no `/data` volume,
  `VKTEAMS_MCP_DATABASE` is gone.
- `save_draft` returns a sealed `draft`; `send_draft` takes it, and with attachments the `files` each upload
  printed. Sending the same draft again posts nothing twice: VK Teams merges a resend with the same
  `localMsgId` (live), and scheduled drafts are checked against the queue, which VK Teams does not merge.
- Disconnecting ends the session in VK Teams. A single issued token can no longer be revoked before it
  expires (access tokens live an hour), a refresh token is not single-use, and the rate allowance is counted
  per replica. A scheduled poll or quote sent again is queued again.
- A test runs a sign-in, a draft and an upload across fresh replicas; CI starts two containers and checks
  that one accepts what the other issued.

## 0.1.0 (2026-11-16)

The first public release.

- 12 tools by default (14 with group administration): chats, messages, people, files, call links and
  bots, about 4,100 tokens of catalogue.
- Streamable HTTP with OAuth 2.1: client registration, PKCE, refresh tokens. A sign-in page in
  Russian and English where the person types the code VK Teams e-mails them; the model never sees it.
- Two-step send, a confirmation flag on every action others see, one rate allowance per person, at most
  20 people per action, mentions included.
- Message ids travel as strings: live ids exceed 2^53, which a JavaScript client rounds.
- A bot's buttons are shown by number and a print; the server presses with the bot's own data and
  refuses a button the bot changed after it was read.
- Files move through signed links that expire in minutes; downloads pass a byte range through.
- Checked on a live VK Teams server (API 139) in three QA rounds; every defect found is fixed and held by
  a test.
