# Requirements

The tool surface, what each tool calls upstream, and every refusal. A row in a **Must refuse** table
names the test that proves it; `tests/unit/test_gates.py::test_every_must_refuse_row_names_a_test_that_exists`
fails when a row names a test that does not exist. `scripts/mutation_check.py` removes the guards behind
most rows (its list names them) and requires their tests to fail; their tests alone hold the rest. Refusals of a malformed argument (empty text, a
poll of one answer, a missing reaction) are covered by the tool tests and are not listed here.

Upstream shapes are the ones recorded in `docs/live-findings.md`. `{base}` is `https://<main-api
host>/api/v<api-version>`, both read from the installation's `myteam-config.json` at sign-in.

## Areas

| Area | Default | Tools |
|---|---|---|
| account | always on | `get_connection` |
| chats | on | `list_chats`, `get_chat`, `update_chat` |
| messages | on | `list_messages`, `save_draft`, `send_draft`, `update_message` |
| people | on | `find_people` |
| files | on | `download_file` (and `files` on `save_draft`, gated) |
| calls | on | `save_call_link` |
| bots | on | `respond_to_bot` |
| chat_admin | **off** | `save_chat`, `update_chat_members` |

## Tools

### get_connection (account, read-only)
Who is signed in (name, address), the installation host and API version, areas on and off, the time
zone. Upstream: `rapi/getUserInfo` on every call; a dead session surfaces as the `reconnect` error.

### list_chats (chats, read-only, list)
Args: `query?`, `filter: all|unread|groups|people` (default all), `limit`, `cursor`. Returns
`{items:[{chat, title, kind: person|bot|group|channel, unread, last_at, last_from, last_message}], …page}`.
`people` is one-to-one chats with people; a bot (an id with no `@`) is `kind: bot`.
Upstream: the chat list is the `buddylist` and `histDlgState` events of one snapshot of the event
stream (the list is not a rapi method); `query` filters titles.

### get_chat (chats, read-only)
Args: `chat` (title, address or id), `include_members?`. Returns title, about and rules
(content_block), kind, member and admin counts, your role, default role, join settings, threads,
unread; with `include_members`, up to 25 members and their roles (no paging). A person or bot chat returns
their profile instead; a bot's id from `list_chats` is accepted as `chat`. Upstream: `rapi/getChatInfo {sn, memberLimit}`, `rapi/getChatMembers {id, pageSize}`,
`rapi/getUserInfo {sn}` (getChatInfo refuses non-group sns: `Invalid chatId`).

### update_chat (chats, notifies others on leave)
Args: `chat`, `action: mark_read|mute|unmute|archive|unarchive|follow_thread|unfollow_thread|leave`,
`minutes?` (mute), `user_confirmed?` (leave). Everything but `leave` is the person's own state.
Upstream: `setDlgState {sn, lastRead}`, `contacts/mute {sn, duration, tieCounter}`,
`archive/chatsModify`, `thread/subscribe|unsubscribe`, wim `buddylist/removeBuddy?buddy&allGroups=1`.

### list_messages (messages, read-only, list)
Args: `chat?`, `query?`, `message?`, `scheduled?`, `limit`, `cursor`. A thread is read by passing its
thread id (from a message's `thread`) as `chat`.
- `chat` alone: history, newest first: `rapi/getHistory {sn, fromMsgId:-1, count:-N, patchVersion:"init"}`;
  `olderMsgId` is the next older message NOT on the page and `fromMsgId` is exclusive, so the cursor carries
  the oldest id shown; `olderMsgId` only says whether there is more.
- `query`: `rapi/searchOneDialog` with `chat`, else `rapi/searchAllDialogs`, `filter:{keyword}`.
- `message`: one message (`fromMsgId: id+1, count: -1`), with reactions and poll results.
- `scheduled`: `message/scheduled/history/get {sn, historyVersion:"0"}` → `messages.new[]`, paged.
Every message: `{id, at, from, text (content_block), mentions, reply_to, files:[{id,name,size}],
buttons:[{button ("1.a3f9c2": its 1-based number and a print of the button), text, url}], reactions:[{emoji, count}], thread, poll, read_by}`. Reactions come from one
`reaction/get` per page; a reaction taken back (counter 0) is not shown. Search results keep the cheaper
`has_reactions` flag; read the message with `message` for its counts.

**Message ids are strings of digits, in arguments and in results.** Live ids exceed 2^53, and a JSON
number that large is rounded by every JavaScript client, which turns an id into its neighbour's. An id
given as a number is refused.

### save_draft (messages)
Args: `chat` (a thread id sends into the thread), `text` (an `@address` in it becomes a mention),
`mention?` (names or addresses), `reply_to?` (message id), `poll_options?`, `send_at?` (local time),
`attach?` (file names to upload; gated by area files).
Returns a preview with the mentions resolved and a sealed `draft` (24 hours) that only `send_draft`
can open; the server keeps nothing. With `attach` it also returns one short-lived upload command per
file. It does not call VK Teams `draft/set`: live 2026-11-15, the client API never returns a draft (not
in history, not in events), and one draft per chat means writing it would overwrite text the person is
typing in their app at that moment.

### send_draft (messages, notifies others)
Args: `chat`, `draft` (from `save_draft`), `files?` (the sealed file each upload printed),
`user_confirmed`. Sends the draft as saved with
`rapi/message/send {target, localMsgId, parts:{mainPart, quoteParts}, schedule?}`. Refuses a draft made
for another person or chat, an altered or expired one, and a draft whose uploads are not all in
`files`. Upload links expire after 600 s; an upload of one link runs once at a time on any replica (a lock in the shared state store) and may be repeated within
the link's life (a lost response); an empty upload is refused (400)
and one over 100 MB too (413); at most three uploads are received at once per replica. Files go first,
at the draft's send time too. Each part has a `localMsgId` fixed in the draft, and VK Teams answers a
resend with the same id with the same message (live), so sending the same draft again, whole or after
a partial failure, posts nothing twice. A scheduled resend is not deduplicated by VK Teams (live), so
for a plain text part the queue is checked first: the same text at the same time already queued is
reported, not queued again. A scheduled poll or quote is not checked (the queue does not show them), and
two replicas sending the same scheduled draft at the same moment can both queue it.

### update_message (messages, notifies others)
Args: `chat`, `message`, `action: edit|delete|pin|unpin|react|unreact|vote|unvote|close_poll|
cancel_scheduled`, `text?` (edit), `reaction?` (react), `option?` (vote), `user_confirmed?`.
Upstream: edit `message/send {updateMsgId}`; delete `delMsgBatch {sn, msgIds, shared:true}`; pin
`pinMessage`; react `reaction/add|remove`; vote `poll/vote|revoke`; close `poll/stop`; cancel
`message/scheduled/cancel {sn, scheduledMsgIds}`.

### find_people (people, read-only, list)
Args: `query` (part of a name, or an address), `limit`. Exact address → one profile with name, about,
last seen and common chats (`getUserInfo`, `getCommonChats`; VK Teams returns no presence status there);
otherwise candidates from `rapi/search {keyword}`.

### download_file (files, read-only)
Args: `file` (the id from a message's `files`, or its link), `chat?`. Returns a signed link
(300 s) the sandbox fetches with `curl -o`, plus name, size, antivirus and DLP status.
Upstream: `GET {base}/files/info/<fileid>/` → `result.info.dlink`, streamed through the link.

### save_call_link (calls)
Args: `title`, `kind: meeting|webinar`, `invite?` (names), `user_confirmed?` (when inviting).
Upstream: `rapi/conference/create {name, type: equitable|webinar, participants, callParticipants}`.

### respond_to_bot (bots, notifies the bot)
Args: `bot`, `action: start|press`, `user_confirmed`, `parameter?` (start), `message?` + `button?`
(press). `button` is what `list_messages` shows: the button's number and a print of its text and data. A
bot's callback data is the bot's own text and could carry instructions, so it never reaches the model; press
reads the message again, refuses if the bot has changed that button since it was read, and sends the data of
that button. A link button is not pressed. The result names the button pressed. Upstream: `startBot {bot, startParameter}`, `getBotCallbackAnswer {chatId,
msgId, callbackData}`.

### save_chat (chat_admin, notifies others)
Args: `user_confirmed` (changes only), `chat?` (absent creates), `title?`, `about?`, `rules?`, `threads?`.
A new group has the person as its only member and lets others join only with an admin's approval
(`approval_to_join`, observed live), so creating it needs no confirmation; it still draws on the person's
allowance. Upstream: `createChat {name, aimSid,
about, live:false, public:false}`, `modChat`.

### update_chat_members (chat_admin, notifies others)
Args: `chat`, `action: add|remove|make_admin|make_member|make_readonly|block|unblock`, `people`
(≤ 20), `user_confirmed`. Upstream: `group/members/add|delete`, `modChatMember`,
`blockChatMembers|unblockChatMembers {members:[{sn}]}`. A `20070` answer with `failures` is a
partial result: the tool reports who failed and why.

## Must refuse

### Contract (every tool)

| Refusal | Test |
|---|---|
| a confirmation that is not the JSON boolean true | tests/unit/test_server.py::test_confirmation_must_be_the_boolean_true |
| an argument the tool does not have | tests/unit/test_server.py::test_an_unknown_argument_is_refused_before_upstream |
| a tool of an area that is off | tests/unit/test_server.py::test_an_off_area_is_not_published_and_cannot_be_called |
| a limit below 1 | tests/unit/test_core.py::test_a_limit_below_one_is_refused |
| a cursor from another tool or person | tests/unit/test_core.py::test_a_cursor_from_another_tool_or_person_is_refused |
| a cursor from the same tool but another list (filter, chat, query) | tests/unit/test_qa_regressions.py::test_a_list_chats_cursor_is_bound_to_its_filter |
| a message id given as a JSON number | tests/unit/test_qa_regressions.py::test_a_message_id_given_as_a_number_is_refused |

### The sealing key

| Refusal | Test |
|---|---|
| starting with no key file | tests/unit/test_key.py::test_a_missing_key_file_refuses_to_start_and_says_why |
| a key file the group or anyone else can read | tests/unit/test_key.py::test_a_key_file_readable_by_others_is_refused |
| a key file owned by another user | tests/unit/test_key.py::test_a_key_file_of_another_user_is_refused |
| starting on Linux when prctl(PR_SET_DUMPABLE) is refused | tests/unit/test_key.py::test_the_server_does_not_start_when_linux_refuses_non_dumpable |
| a key path that is not a regular file (a FIFO would block the start) | tests/unit/test_key.py::test_a_key_path_that_is_a_fifo_is_refused_without_blocking |
| a key shorter than 32 characters | tests/unit/test_key.py::test_a_short_key_in_the_file_is_refused |
| a key file that is a directory or unreadable | tests/unit/test_key.py::test_a_key_path_that_is_not_a_readable_file_says_so_without_a_traceback |
| a key file that is not UTF-8 text | tests/unit/test_key.py::test_a_key_that_is_not_text_is_refused |
| a key longer than 4096 bytes, rather than a shortened one | tests/unit/test_key.py::test_a_key_longer_than_the_limit_is_refused_not_shortened |
| a token, refresh or registration sealed under a previous deploy's key | tests/unit/test_key.py::test_a_new_key_signs_everyone_out_once_and_they_sign_in_again |

### The shared state store (many replicas)

| Refusal | Test |
|---|---|
| an authorization code redeemed twice, on one replica or on two | tests/unit/test_oauth.py::test_a_code_is_used_once, tests/unit/test_replicas_on_valkey.py::test_a_code_redeemed_on_one_replica_is_refused_on_the_other |
| starting with VKTEAMS_MCP_SECRET set | tests/unit/test_key.py::test_a_secret_variable_is_refused_and_the_error_names_it_and_the_migration |
| a state store URL together with Sentinels | tests/unit/test_state_config.py::test_a_url_and_sentinels_together_are_a_config_error |
| a password in the state store URL | tests/unit/test_state_config.py::test_a_password_in_the_url_is_refused_and_not_echoed |
| a state store password file others can read | tests/unit/test_state_config.py::test_a_password_file_others_can_read_is_refused |
| sending while the state store is down | tests/unit/test_state_failclosed.py::test_sending_is_refused_and_nothing_is_sent |
| signing in while the state store is down | tests/unit/test_state_failclosed.py::test_sign_in_refuses_a_step_it_cannot_count |
| a code exchange the store cannot record (the code stays unspent) | tests/unit/test_state_failclosed.py::test_the_token_endpoint_answers_503_and_leaves_the_code_unspent |
| an upload the store cannot lock | tests/unit/test_state_failclosed.py::test_an_upload_is_refused_with_503_and_nothing_reaches_vk_teams |
| a sixth wrong code for one sign-in step, across replicas | tests/unit/test_replicas_on_valkey.py::test_wrong_codes_are_counted_across_replicas |
| a fourth code request for one address, across replicas | tests/unit/test_replicas_on_valkey.py::test_code_requests_are_limited_across_replicas |
| an eleventh outward action, across replicas | tests/unit/test_replicas_on_valkey.py::test_the_send_allowance_is_one_bucket_across_replicas |
| a second upload of a link while one runs | tests/unit/test_files.py::test_a_link_is_busy_while_an_upload_of_it_runs_and_free_after |
| a login, address or IP in a state store key | tests/unit/test_state_contract.py::test_no_key_carries_a_login_an_address_or_an_ip |
| a log record that looks like content | tests/unit/test_logs.py::test_a_record_that_looks_like_content_is_dropped_and_counted |
| an exception message in a log line | tests/unit/test_logs.py::test_a_traceback_is_frames_and_types_never_messages |
| a log line that is not JSON | tests/unit/test_logs.py::test_the_running_server_writes_only_json_from_start_to_stop |
| a person, address or tool argument as a metric label | tests/unit/test_metrics.py::test_no_label_carries_a_person_an_address_a_token_or_an_argument |
| /metrics on the public port | tests/unit/test_metrics.py::test_the_public_port_has_no_metrics |
| a per-network limit counting a whole company as one address | tests/unit/test_platform.py::test_the_limit_per_network_is_off_without_trusted_proxies_and_on_with_them |

### Sending and changing

| Refusal | Test |
|---|---|
| send_draft without user_confirmed | tests/unit/test_messages.py::test_send_draft_needs_confirmation |
| send_draft with a draft that save_draft did not make for this chat | tests/unit/test_messages.py::test_send_draft_without_a_saved_draft |
| send_draft with a draft of another person, an altered one or one older than 24 hours | tests/unit/test_replicas.py::test_a_draft_of_another_person_altered_or_expired_is_refused |
| a draft too large to seal | tests/unit/test_replicas.py::test_save_draft_refuses_a_draft_too_large_to_seal |
| reporting a disconnect as done while VK Teams is down | tests/unit/test_replicas.py::test_a_disconnect_while_vk_teams_is_down_is_reported |
| a poll answer over 300 characters | tests/unit/test_replicas.py::test_a_long_poll_answer_is_refused_at_save |
| send_draft while an announced file has not been uploaded | tests/unit/test_messages.py::test_send_draft_waits_for_announced_files |
| an 11th outward action within the bucket | tests/unit/test_core.py::test_the_bucket_refuses_the_eleventh_action |
| edit or delete of someone else's message | tests/unit/test_messages.py::test_only_own_messages_change |
| a name that matches several chats | tests/unit/test_chats.py::test_an_ambiguous_chat_lists_candidates |
| more than 20 people in one action | tests/unit/test_admin.py::test_more_than_twenty_people_is_refused |
| leave without user_confirmed | tests/unit/test_chats.py::test_leave_needs_confirmation |
| an attachment given as a path, a hidden file or an option (`../x`, `/etc/x`, `~/.ssh/x`, `.env`, `-rf`) | tests/unit/test_qa_live_scenarios.py::test_an_attachment_is_a_file_name_in_the_current_folder_not_a_path |
| a reaction that is not one emoji | tests/unit/test_qa_live_scenarios.py::test_a_reaction_that_is_not_one_emoji_is_refused_before_vk |
| cancelling a scheduled message that is not in the chat's schedule | tests/unit/test_qa_live_scenarios.py::test_cancelling_a_scheduled_message_that_is_not_there_is_refused |
| changing a group without user_confirmed | tests/unit/test_qa_regressions.py::test_changing_a_group_still_needs_confirmation |
| a 11th group created within the bucket | tests/unit/test_qa_regressions.py::test_creating_groups_is_rate_limited |
| more than 20 people mentioned in one message (by name, inline address or native `@[…]`) | tests/unit/test_qa_attacks.py::test_mentions_are_held_to_the_people_cap, ::test_inline_addresses_are_held_to_the_people_cap, ::test_native_mention_syntax_is_held_to_the_people_cap |
| a poll answer that is empty | tests/unit/test_qa_attacks.py::test_a_poll_with_an_empty_answer_is_refused_at_save |
| two poll answers that are the same (ignoring case and spaces) | tests/unit/test_qa_attacks.py::test_a_poll_with_the_same_answer_twice_is_refused_at_save |
| a send time more than 366 days ahead | tests/unit/test_qa_attacks.py::test_a_send_time_decades_ahead_is_refused_at_save |
| an edit longer than 10 000 characters | tests/unit/test_qa_attacks.py::test_an_edit_is_held_to_the_text_limit |
| a message id past 64 bits | tests/unit/test_qa_attacks.py::test_a_message_id_beyond_64_bits_is_refused_before_vk |
| a button number the message does not have | tests/unit/test_qa_attacks.py::test_a_button_number_outside_the_keyboard_is_refused_before_vk |
| pressing a link button | tests/unit/test_qa_attacks.py::test_a_link_button_is_not_pressed |
| pressing a button the bot changed after it was read | tests/unit/test_qa_attacks.py::test_a_keyboard_the_bot_changed_after_it_was_read_is_not_pressed |
| an edit mentioning more than 20 people | tests/unit/test_qa_attacks.py::test_an_edit_is_held_to_the_people_cap |
| a poll answer or a message of invisible characters only (U+200B, U+2060, BOM) | tests/unit/test_qa_attacks.py::test_a_poll_answer_of_invisible_characters_is_refused |

### Sign-in and the public server

| Refusal | Test |
|---|---|
| a server address that is http, an IP, or resolves to a private network | tests/unit/test_install.py::test_private_and_plain_addresses_are_refused |
| a host that does not serve a VK Teams config | tests/unit/test_install.py::test_a_host_without_vk_config_is_refused |
| a host outside the operator's pinned list | tests/unit/test_install.py::test_pinned_installations_only |
| a 4th code request for one address within an hour | tests/unit/test_signin.py::test_code_requests_are_limited |
| an altered access token, or a token of another kind | tests/unit/test_oauth.py::test_an_altered_token_is_refused |
| an expired access token | tests/unit/test_oauth.py::test_tokens_expire |
| a code or refresh token of another client | tests/unit/test_oauth.py::test_a_code_belongs_to_its_client |
| a code exchange with a wrong PKCE verifier | tests/unit/test_http.py::test_a_wrong_pkce_verifier_is_refused |
