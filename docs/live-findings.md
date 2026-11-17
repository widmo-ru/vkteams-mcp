# Live findings

What a real VK Teams on-premise server (API version 139) did, measured on 2026-11-15 and, for the rows naming `test_qa_regressions.py`, on 2026-11-16. The client API
is undocumented; every row here was observed live. Where a row names a unit test, its fake answers in the
measured shape; rows that name a probe or the live suite (`tests/live`) are checked only against a real
server, by hand.
Where the reverse-engineered descriptions in circulation say otherwise, this file wins.

## Paths and errors

| Finding | Test |
|---|---|
| The prefix is `/api/v<api-version>/`, with the version from `GET /myteam-config.json` (public). `/api/v/rapi/*` answers `40000 Unknown method` for every method. | `test_api.py::test_rapi_puts_aimsid_in_the_json_body_under_the_versioned_prefix` |
| rapi errors are HTTP 200 with `status.code`: `200xx` success (`20001` "History is empty", `20020` "Scheduled message stored to queue"), `20070` partial failure with `results.failures[{id, error}]`, `20080` accepted, answered later, `40000` bad request, `40001` refused, `40200` bad token, `40400` bad target, `50000` server error. | `test_api.py` |
| `50000` on a read passes: the same search answered `20000` a minute later. Reads are retried once, writes never. | `test_api.py::test_a_passing_server_error_on_a_read_is_tried_once_more` |
| Parameters are JSON-schema validated before the token: an unauthenticated call names missing fields (`required property 'X'`). | live probe |
| wim errors are HTTP 200 with `response.statusCode` (401 = session, renewed once). | `test_api.py::test_when_renewal_is_refused_the_person_must_sign_in_again` |
| `aimsid` counts only in the rapi JSON body; in the query or an `X-Teams-Aimsid` header it is ignored. | live suite `test_auth.py` |
| `httpx` logs full URLs at INFO, and wim URLs carry the `aimsid`. | `test_gates.py::test_a_full_message_flow_logs_no_content` |

## Sign-in

| Finding | Test |
|---|---|
| `clientLogin s=<address> pwd=none tokenType=otp_via_email` answers 200 with an empty token and e-mails a 6-character upper-case code. | `test_signin.py::test_requesting_a_code_sends_the_desktop_request` |
| `clientLogin s pwd=<CODE>` (no tokenType) gives `token.a`, `sessionSecret`, `hostTime`; `expiresIn` is 0 (long-term). | `test_signin.py::test_finishing_sign_in_never_asks_for_another_code` |
| Sending the code with `tokenType=otp_via_email` again e-mails a NEW code and invalidates the typed one. A wrong code without it answers 330 and sends nothing. | `test_signin.py::test_a_wrong_code_is_refused_and_nothing_is_resent` |
| `devId` (alias `k`) is required: `460 Missing required parameter (k)`. | `test_signin.py` |
| `startSession` needs `sig_sha256 = b64(HMAC-SHA256(sessionKey, "POST&"+pct(url)+"&"+pct(sorted query)))`, `sessionKey = b64(HMAC-SHA256(code, sessionSecret))`. | `test_api.py::test_the_signature_matches_the_desktop_algorithm` |
| A non-existent address also gets "code sent": the server does not reveal accounts. | `test_signin.py::test_a_refused_code_request_does_not_say_whether_the_address_exists` |

## Reading

| Finding | Test |
|---|---|
| The chat list is not a rapi method: it is the `buddylist` and `histDlgState` events of the event stream (recent dialogs with `unreadCnt`, last message, persons). | `test_chats.py` |
| `fetchBaseURL` is a cursor: each answer carries the next one. Polling the session's stored one again replays the initial events (chat list, recent dialogs), which `list_chats` uses. | `test_api.py::test_events_follow_the_next_fetch_url`, `test_chats.py` |
| `getHistory` needs `patchVersion` (`"init"`); `fromMsgId` is EXCLUSIVE: `count 1` gives the next newer message, `count -1` the previous one. One message X is `fromMsgId X+1, count -1`. | `test_messages.py::test_one_message_is_fetched_by_its_own_id` |
| `olderMsgId` is the next older message that is NOT on the page (absent when there is none). Paging from it with an exclusive `fromMsgId` drops one message per page; the next page starts from the oldest id shown. | `test_qa_live_scenarios.py::test_paging_through_history_returns_every_message_once` |
| `reaction/get {chatId, msgIds}` → `reactions[{msgId, chatId, reactions[{reaction, counter}]}]`; a taken-back reaction stays with `counter 0`, and history keeps `reactions.exist` true. A reaction that is not an emoji makes the server fail (`50000`). | `test_qa_live_scenarios.py::test_history_shows_reaction_counts_and_nothing_once_they_are_taken_back` |
| Search takes `filter: {keyword}`; any other shape answers "Jimny error". `searchChatThreads` takes `sn`, not `chatId`. | `test_messages.py::test_search_in_all_chats_uses_a_keyword_filter` |
| Directory search is `rapi/search {keyword}` → `data[{sn, anketa{friendly}}]`. | `test_more_tools.py::test_find_people_searches_the_directory` |
| `msgId` runs past 2^53 (ids look like `7700000000000000301`). A JSON number that large reaches a JavaScript client as its neighbour, so ids must travel as strings. | `test_qa_regressions.py::test_message_ids_come_out_as_exact_strings` |
| People are e-mail addresses and groups `<n>@chat.agent`; a bot's dialog id has no `@` (`70001`), and its `persons` entry carries no bot flag. | `test_qa_regressions.py::test_a_bot_is_listed_as_a_bot_not_a_person` |
| A bot often answers with an inline keyboard (`inlineKeyboardMarkup[[{text, callbackData, style}]]`): its text alone ends at "the commands:". | `test_qa_regressions.py::test_a_bots_buttons_are_shown_in_the_history` |
| `getChatInfo` refuses a person (`Invalid chatId`); the role is `you.role`. | `test_chats.py::test_a_chat_is_found_by_part_of_its_title` |
| Attachments are `filesharing[{id, name, size (string), mime, original_url, antivirus_check}]`. | `test_messages.py::test_attachments_come_with_an_id_the_download_tool_takes` |
| Drafts are never returned to the client API (not in history, not in events). | requirements, `save_draft` |

## Writing

| Finding | Test |
|---|---|
| `message/send` takes `parts` as an OBJECT `{mainPart:{text:{plain}}, quoteParts:[…]}`; a wrong shape answers `40000 DLP error - Invalid request`, and the right shape with the same text goes through. | `test_messages.py::test_send_draft_sends_exactly_the_saved_text_once` |
| Mentions are `@[<sn>]` in the text; the server parses them. | `test_messages.py::test_mentions_are_resolved_by_name_into_the_text` |
| `schedule: {scheduledTime: <unix seconds>}` queues the message (`20020`); `message/scheduled/history/get` returns `messages.new[]`. | `test_messages.py::test_a_scheduled_draft_is_queued` |
| A send time a year ahead is accepted; decades ahead are refused only at send ("Scheduled time is too far in the future"). An empty poll answer is refused only at send as well. A message id past 64 bits answers "Invalid json". | live, QA round 2 (`docs/qa-scenarios.md`); the local guards: `test_qa_attacks.py` |
| An edit that writes 21 `@[address]` makes VK Teams record 21 mentions; a poll answer of U+200B alone is accepted. A bot edits its own message when a button is pressed (its keyboard can change between reading and pressing). | live, QA round 3 (`docs/qa-scenarios.md`) |
| `message/send` with a `localMsgId` VK Teams has seen answers with the same `msgId` and posts nothing new. A scheduled send is not merged: the same request twice queues two messages, and the queue (`scheduledMsgId`, `scheduledTime`, `parts`) does not show the `localMsgId`. | live probe 2026-11-16 (two sends of one request each way); the unit tests `test_send_draft_sends_exactly_the_saved_text_once` and `test_a_scheduled_draft_sent_again_is_not_queued_twice` hold the code to it |
| `message/scheduled/cancel` answers success for an id the chat does not have. | `test_qa_live_scenarios.py::test_cancelling_a_scheduled_message_that_is_not_there_is_refused` |
| `contacts/mute` refuses a long duration (`Mute duration too long` for 2^31-1 s); the apps mute for good with wim `buddylist/Mute?buddy=<sn>&eternal=1`, and `contacts/mute duration 0` undoes it. | `test_qa_live_scenarios.py::test_mute_without_minutes_is_an_eternal_mute` |
| The file server answers a range past the end with `416` and no `Content-Range`. | `test_qa_live_scenarios.py::test_a_range_past_the_end_names_the_length_even_when_vk_does_not` |
| A poll is `mainPart {text, poll:{type:"anon", responses}}`; `poll/get` gives `answers[{answerId, text, votes}]`. | `test_messages.py::test_a_poll_needs_two_to_ten_answers` |
| Files: `GET /files/init` → `{host, url}` → `POST https://<host><url>` with `Session-ID`, `Content-Range` → `{fileid, static_url}`; `GET /files/info/<id>/` answers `20000` (init and upload answer `200`) with a signed `dlink`. A file is posted as its link. | `test_files.py` |
| `group/members/add` of a bot that may not join groups: `20070`, `bot_setjoingroups_false`. | `test_admin.py::test_partial_failures_are_reported_per_person` |
| Leaving a chat: wim `buddylist/removeBuddy?buddy=<sn>&allGroups=1`; without `allGroups` it is 400. There is no delete for a group. | `test_chats.py::test_leave_uses_remove_buddy_with_all_groups` |
| `getChatInfo.live` is the live-chat mode (in the apps some actions become admin-only), not joining by link: the server sets it on every new group whatever `createChat` says, and refuses it in `modChat` (`additional property 'live'`). Joining is governed by `controlled` (approval), which a new group has on. | `test_qa_regressions.py::test_get_chat_does_not_call_live_chat_mode_join_by_link` |

## Deployment

| Finding | Consequence |
|---|---|
| Many corporate installations resolve to private addresses and are reachable only inside the company network. | A public cloud connector cannot reach them; the same image runs inside the network with `VKTEAMS_MCP_ALLOW_PRIVATE_NETWORKS=1` and the installation pinned. |
| An older session's event stream can replay the group list and no dialog states: every one-to-one chat disappears from the list. A new session (`startSession`) starts with all of them (production, 2026-11-17). | The chat list opens a new session whenever the stream has no dialog states, then reads it again (`test_a_stream_without_dialog_states_renews_the_session`). |
