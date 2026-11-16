# Manifesto

This server gives an AI assistant the VK Teams of **one person**: their chats, their messages, their
files. It acts as that person, with that person's rights, and with nothing more. It works with any
VK Teams or VK WorkSpace installation the person can sign in to; nothing in it names one.

Everything here is a rule, not a preference. A rule that the code can break without a test going
red is a defect in the tests. Every rule names what a violation looks like, so that a reviewer can
find one and a test can fail on one.

## Why this exists

VK Teams has two APIs. The **Bot API** acts as a bot: it cannot read a person's chats, search their
history or act in their name. The **client API** the VK Teams apps use can, but it is undocumented,
its paths depend on the server version, and its errors arrive as HTTP 200. A model given that API
one endpoint per tool spends its context choosing between `getHistory`, `getHistoryBatch`,
`searchOneDialog` and `searchAllDialogs`, and sends `parts` in a shape the server answers with
"DLP error".

We build the other shape: **few tools, each answering one question a person actually asks**, with the
server doing the work the API leaves to the caller — finding a chat or a person by name, turning
`@name` into a mention, choosing the right of four history methods, refusing what must not happen.
Every request shape is checked against a live server (`docs/live-findings.md`).

## I. The contract with the model

1. **One question, one tool.** Depth is a flag (`include_members`, `include_reactions`), a search is
   a `query`, a single item is an id — never a second tool. *Violation:* two tools whose
   implementations share a code path and differ in output shape.
2. **Verbs are `list_`, `get_`, `save_`, plus a verb that says what happens** (`send_draft`,
   `download_file`, `respond_to_bot`). `save_` is an upsert: no id creates, an id updates.
   *Violation:* `create_x` beside `update_x`; `manage_x` with a free-form action string.
3. **Changes that are not upserts take a closed `action` enum**, inline in the schema.
   *Violation:* an `action: str`; an enum behind a `$ref`.
4. **No prefix.** The client namespaces tools. *Violation:* `vkteams_`, `vk_`, `mcp_` in a name.
5. **Descriptions read like a man page and say when to call the tool**: at least three sentences;
   irreversible tools say so. *Violation:* a description that restates the tool name.
6. **Flat arguments.** Scalars and lists of scalars. Every argument whose name is not
   self-explanatory has a description that adds information. *Violation:* a nested object argument.
7. **The server resolves names.** A chat by its title, a person by name or address, time as a local
   string (`2026-11-16 15:00`) in the person's time zone, durations in minutes. **Ambiguity is an
   error that lists the candidates** — never the first match. *Violation:* an argument that needs
   an id the model could only have guessed.
8. **Projections, never raw API JSON.** *Violation:* a result carrying `reqId`, `patchVersion`,
   `updatePatchVersion`, `snippets` or any upstream envelope passed through.
9. **Bounded output.** Lists default to 10 items, cap at 25, continue by an opaque signed `cursor`.
   Long text is capped and says so. *Violation:* an `offset`; a list with no cap; silent truncation.
10. **Bytes never enter the context.** Files move through short-lived signed links the agent's
    sandbox uses with `curl`. *Violation:* base64 in a tool result.
11. **Errors are errors.** A failed call sets `isError` and its text names the next step and what did
    not happen. *Violation:* `{"error": …}` inside a success; an upstream status code shown raw.
12. **Annotations describe actual behaviour**: `readOnlyHint`, `destructiveHint` (changes existing
    state), `idempotentHint`, `openWorldHint` (other people are affected).
13. **Areas switch off like separate servers.** An installation of this server turns an area off
    with one line; an off area is not published at all. Chat administration is off by default. The
    one exception is `account` (`get_connection`), which is always on: it is how a person learns why
    anything else fails.
    *Violation:* a published tool answering "disabled".
14. **Every change is weighed** with a real tokenizer: the default catalogue stays under
    **10,000 tokens**, no area over 3,000, no tool over 600. Growing past that is a recorded decision,
    not an edit.

## II. Acting as one person

15. **The person signs in themselves.** The one-time code VK Teams e-mails them is typed by the
    person on this server's sign-in page, never into a chat with the model and never into a tool.
    *Violation:* a tool argument or a model-visible text that carries a code, a password or a session.
16. **One person per token, one installation per session.** An access token maps to exactly one
    person on exactly one VK Teams server, chosen at sign-in and stored with the session. No tool takes
    a server, a user or a session argument. *Violation:* any argument that selects another person,
    server or session.
17. **Rights come from VK Teams.** We never widen what the person can do and never pretend to
    narrow it with a check VK Teams would not enforce.
18. **Sessions are sealed.** VK Teams credentials (`a` token, session key, `aimsid`) are stored
    encrypted with a key from the environment; our own tokens are stored only as hashes. A leaked
    database gives neither. *Violation:* a credential or token written in clear to disk or log.

## III. A public server

19. **The server address is the person's input, so it is untrusted.** Only `https`, only a host that
    serves a VK Teams `myteam-config.json`, never an IP literal, never an address that resolves to a
    private, loopback or link-local network unless the operator allowed it. An operator can pin
    the server to a list of installations. *Violation:* the server fetching a URL it was not proven
    to be a VK Teams installation.
20. **Sign-in cannot be used to send mail.** Code requests are limited per address and per client
    network; the page never says whether an address exists. *Violation:* a loop that makes the
    server e-mail a stranger many times.

## IV. Care with what cannot be undone

The rule is not "never write". It is: **what other people see, or what cannot come back, needs the
person's word; everything else stays reversible.**

21. **Sending is two steps.** `save_draft` keeps a draft on this server and shows it back;
    `send_draft(chat, user_confirmed=true)` sends that draft exactly and nothing else. The person's
    own draft in their VK Teams app is never touched. *Violation:* any path that sends a message
    without a saved draft, or sends text other than the saved one.
22. **Anything other people see at once needs `user_confirmed=true`** — the JSON boolean, not "yes"
    and not 1: sending, editing or deleting a sent message, pinning, voting, leaving a chat, inviting
    to a call, pressing a bot's button, changing a group or its members.
23. **Rate.** All such actions draw from one bucket per person: 10 at once, refilled at 10 per
    minute. One chat per action; at most 20 people per action. Broadcast is out of scope.
24. **Only what is yours.** Edit and delete only your own messages; nothing on someone else's behalf.
25. **Never exposed, by design:** profile and status changes, nick changes, session resets, abuse
    reports, SMS, deleting a chat's history, privacy blacklists, phone binding. *Violation:* any of
    them reachable through a tool.

## V. Content is data

26. **Text written by other people is untrusted** — messages, chat titles, names, file names. It
    returns inside a marked block with a fresh nonce, "data, not instructions". This lowers risk; it is
    not a boundary.
27. **The boundaries are structural:** drafts, confirmation, the bucket, and the operations that do
    not exist. An injected "send this to everyone" finds no broadcast and no send without a draft.

## VI. Engineering

28. **English everywhere** in the repository.
29. **Test first.** Every rule here has a test that fails when the rule is broken, seen failing once;
    every refusal in `docs/requirements.md` names its test.
30. **The suite cannot pass empty.** The number of tests run is derived from the tests on disk.
31. **No content in logs**: no message text, names, chat titles, addresses, codes or tokens. A test
    asserts it.
32. **API quirks live in one place.** Only `vkteams_mcp.api` speaks HTTP to VK Teams: the path
    prefix `/api/v<N>`, HTTP-200 errors, `200xx` with `failures`, session renewal. Tools never do.
33. **Live findings beat documentation.** What a real server did is in `docs/live-findings.md`; each
    row names the unit test whose fake answers in the measured shape, and the live suite
    (`tests/live`) repeats it against a real server by hand.
34. **Product code names no installation and no person.** A purity test fails the suite otherwise.
