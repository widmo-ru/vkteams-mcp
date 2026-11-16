# Changelog

## 0.1.0 (2026-11-16)

The first public release.

- 12 tools by default (14 with group administration): chats, messages, people, files, call links and
  bots, about 4,100 tokens of catalogue.
- Streamable HTTP with OAuth 2.1: client registration, PKCE, rotating refresh tokens. A sign-in page in
  Russian and English where the person types the code VK Teams e-mails them; the model never sees it.
- Two-step send, a confirmation flag on every action others see, one rate allowance per person, at most
  20 people per action, mentions included.
- Message ids travel as strings: live ids exceed 2^53, which a JavaScript client rounds.
- A bot's buttons are shown by number and a print; the server presses with the bot's own data and
  refuses a button the bot changed after it was read.
- Files move through signed links that expire in minutes; downloads pass a byte range through.
- Checked on a live VK Teams server (API 139) in three QA rounds; every defect found is fixed and held by
  a test.
