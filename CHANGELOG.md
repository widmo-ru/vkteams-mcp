# Changelog

## 0.2.6 (2026-11-18)

From the first production report.

- One-to-one chats no longer vanish from the chat list. An older session's event stream replayed the
  group list without dialog states; the server now opens a new session whenever the stream carries no
  dialog states, not only when it is empty.
- A chat can be named by a colleague's name even when it is not in the chat list yet: the directory is
  searched, as `find_people` does, and several matches are offered rather than guessed. "Избранное",
  "Favorites" and "Saved messages" name the person's own chat.
- The image is built on Alpine and has no HIGH or CRITICAL findings in Trivy; the Debian base carried 101,
  none with a fix from Debian. CI now fails on any such finding.
- The image is published as a plain Docker manifest list with linux/amd64 and linux/arm64 and nothing else.
  The provenance entries of earlier releases made a Nexus proxy offer only arm64.

## 0.2.5 (2026-11-16)

The open QA remarks.

- Search across all chats reports `chats_matched` instead of a `total` that counted chats, not messages,
  and stops offering a cursor once every matching chat has come.
- Voting checks the poll first: a closed poll and an option the poll lacks are refused by name, before the
  action allowance is spent.
- `find_people` says when there are more people than shown, marks bots as `kind: bot`, refuses an empty
  query and says how many chats a person shares when it lists only ten.
- `list_chats` follows the event stream past the session's first batch, so `last_at`, unread counts and last
  messages show what happened since the session began (live: `last_at` stayed hours behind).
- Sending a draft that went out before says so: `already_sent` with a note. VK Teams answers such a resend with
  the first message's id and time, even if that message was deleted, and posts nothing new.
- A call needs a title. A damaged cursor, garbled or with an altered signature, is called damaged. `get_chat` with `include_members` on a
  one-to-one chat says who its members are instead of ignoring the request.

## 0.2.4 (2026-11-16)

From the sixth QA round, a full regression of 158 scenarios with no failures.

- A sealed link, token or draft has one spelling: the spare bits of its last character must be zero, so
  four spellings of one link no longer all work.
- A reaction that is not one emoji is refused as such, not as a missing reaction.

## 0.2.3 (2026-11-16)

Fixes from the fifth QA round.

- Control characters, private-use and unassigned code points count as invisible: a poll answer, a message
  or an edit made only of them is refused. Live, a poll answer of U+0001 alone was sent and kept. Emoji
  blocks stay visible, so an emoji newer than the server's Unicode database is not taken for empty.
- A reply to someone else's message names the quoted author among the draft's mentions and counts them
  towards the 20-people cap: VK Teams notifies them (live: 20 mentions and a quote recorded 21).

## 0.2.2 (2026-11-16)

Fixes from the fourth QA round.

- A mention written with spaces inside, `@[ address ]`, counts towards the 20-people cap in drafts and
  edits and is listed in the draft preview. VK Teams treats it as a mention (live: an edit with 21 of them
  notified 21 people).
- An edit to invisible characters only is refused, as a draft already was. Hangul fillers (U+3164 and
  its kin) and the blank Braille pattern (U+2800) now count as invisible in drafts, polls and edits.
- A bot button's print is keyed by the server's secret. The unkeyed 24-bit print let a bot find another
  button with the same print in under a minute and have it pressed. Its fields are encoded one by one,
  so moving a line from the label into the data changes the print.
- `@someone@example.com` in an edit becomes a mention, as in a draft, and counts towards the cap.

## 0.2.1 (2026-11-16)

- A client the server no longer knows (registered with 0.1, or before the secret changed) used to get
  raw JSON from `/authorize`. The browser now shows a page in the person's language that says to clear
  the connector's authorization in the AI app and connect again. Redirects are unchanged.

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
