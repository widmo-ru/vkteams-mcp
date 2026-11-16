"""Remove each safety check in turn and require the tests that hold it to fail.

A test that still passes with its guard removed was not testing the guard. A mutation whose text is
no longer in the file fails too, so a refactor cannot quietly retire a check.

    uv run python scripts/mutation_check.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "vkteams_mcp"

# (what, file, [(original, replacement)], tests that must fail)
MUTATIONS = [
    (
        "the person's word",
        "core/guard.py",
        [("if user_confirmed is not True:", "if False:")],
        [
            "tests/unit/test_core.py::test_the_guard_needs_the_boolean_true",
            "tests/unit/test_messages.py::test_send_draft_needs_confirmation",
        ],
    ),
    (
        "people per action",
        "core/guard.py",
        [("if people > MAX_PEOPLE:", "if False:")],
        ["tests/unit/test_core.py::test_more_than_the_people_limit_is_refused_before_the_bucket"],
    ),
    (
        "the bucket",
        "core/guard.py",
        [("if b.tokens < 1:", "if False:")],
        [
            "tests/unit/test_core.py::test_the_bucket_refuses_the_eleventh_action",
            "tests/unit/test_messages.py::test_the_bucket_holds_across_tools",
        ],
    ),
    (
        "confirmation must be a boolean",
        "server.py",
        [("if p in args and not isinstance(args[p], bool):", "if False:")],
        ["tests/unit/test_server.py::test_confirmation_must_be_the_boolean_true"],
    ),
    (
        "unknown arguments",
        "server.py",
        [("if unknown:", "if False:")],
        ["tests/unit/test_server.py::test_an_unknown_argument_is_refused_before_upstream"],
    ),
    (
        "only own messages",
        "tools/messages.py",
        [('if not m.get("outgoing"):', "if False:")],
        ["tests/unit/test_messages.py::test_only_own_messages_change"],
    ),
    (
        "private networks",
        "api/install.py",
        [("if not ip.is_global or ip.is_multicast:", "if False:")],
        ["tests/unit/test_install.py::test_a_host_resolving_to_a_private_network_is_refused"],
    ),
    (
        "pinned installations",
        "api/install.py",
        [("if policy.installations and host not in policy.installations:", "if False:")],
        ["tests/unit/test_install.py::test_pinned_installations_only"],
    ),
    (
        "no redirects",
        "api/install.py",
        [("follow_redirects=False, timeout=10", "follow_redirects=True, timeout=10")],
        ["tests/unit/test_install.py::test_redirects_are_not_followed"],
    ),
    (
        "code requests are limited",
        "api/login.py",
        [("if self._hit(a, CODES_PER_ADDRESS_PER_HOUR) or", "if False and")],
        ["tests/unit/test_signin.py::test_code_requests_are_limited"],
    ),
    (
        "spaced native mentions count",
        "tools/messages.py",
        [
            (
                '_NATIVE_MENTION = re.compile(r"@\\[([^\\[\\]]+)\\]")',
                '_NATIVE_MENTION = re.compile(r"@\\[([^\\[\\]\\s]+)\\]")',
            )
        ],
        ["tests/unit/test_qa_attacks.py::test_spaced_mentions_in_an_edit_are_held_to_the_people_cap"],
    ),
    (
        "button fields are encoded one by one",
        "tools/_common.py",
        [
            (
                'json.dumps([str(b.get(k, "")) for k in ("text", "callbackData", "url")], ensure_ascii=False).encode()',
                '"\\n".join(str(b.get(k, "")) for k in ("text", "callbackData", "url")).encode()',
            )
        ],
        ["tests/unit/test_qa_attacks.py::test_moving_text_between_a_buttons_fields_changes_its_print"],
    ),
    (
        "blank glyphs are invisible",
        "tools/messages.py",
        [("        and ch not in _BLANK_GLYPHS\n", "")],
        ["tests/unit/test_qa_attacks.py::test_blank_letters_are_invisible_in_a_draft_and_an_edit"],
    ),
    (
        "controls and unassigned are invisible",
        "tools/messages.py",
        [
            (
                '_INVISIBLE = frozenset({"Cc", "Cf", "Cn", "Co", "Cs", "Mn", "Me", "Zs", "Zl", "Zp"})',
                '_INVISIBLE = frozenset({"Cf", "Mn", "Zs"})',
            )
        ],
        ["tests/unit/test_qa_attacks.py::test_a_poll_answer_of_control_or_unassigned_characters_is_refused"],
    ),
    (
        "new emoji stay visible",
        "tools/messages.py",
        [
            (
                "(unicodedata.category(ch) not in _INVISIBLE or _is_emoji_base(ch))",
                "unicodedata.category(ch) not in _INVISIBLE",
            )
        ],
        ["tests/unit/test_qa_attacks.py::test_an_emoji_newer_than_the_servers_unicode_is_visible"],
    ),
    (
        "the quoted author counts as mentioned",
        "tools/messages.py",
        [("            notified.add(author)\n", "")],
        ["tests/unit/test_qa_attacks.py::test_a_reply_counts_the_quoted_author_towards_the_people_cap"],
    ),
    (
        "a sealed blob has one spelling",
        "core/seal.py",
        [("            if _b64(raw) != blob:", "            if False:")],
        ["tests/unit/test_qa_attacks.py::test_a_sealed_blob_has_one_spelling"],
    ),
    (
        "a call needs a title",
        "tools/calls.py",
        [("        if not _visible(title):", "        if False:")],
        ["tests/unit/test_qa_attacks.py::test_a_call_needs_a_title"],
    ),
    (
        "a blank people query is refused",
        "tools/people.py",
        [("        if not q:", "        if False:")],
        ["tests/unit/test_qa_attacks.py::test_a_blank_people_query_is_refused"],
    ),
    (
        "find_people says there are more",
        "tools/people.py",
        [("        more = len(found) > n", "        more = False")],
        ["tests/unit/test_qa_attacks.py::test_find_people_says_when_there_are_more"],
    ),
    (
        "a vote names the options",
        "tools/messages.py",
        [('if action == "vote" and ids and str(option) not in ids:', "if False:")],
        ["tests/unit/test_qa_attacks.py::test_a_vote_for_an_option_the_poll_lacks_is_refused_before_vk"],
    ),
    (
        "a closed poll is named",
        "tools/messages.py",
        [('                if state.get("closed"):', "                if False:")],
        ["tests/unit/test_qa_attacks.py::test_a_closed_poll_is_named_as_closed"],
    ),
    (
        "search stops when every chat was seen",
        "tools/messages.py",
        [("and not (isinstance(total, int) and seen >= total)", "")],
        ["tests/unit/test_qa_attacks.py::test_search_in_all_chats_counts_chats_and_stops_when_they_are_seen"],
    ),
    (
        "a resend is named",
        "tools/messages.py",
        [('        out["already_sent"] = True\n', "")],
        ["tests/unit/test_qa_attacks.py::test_a_draft_sent_before_says_so"],
    ),
    (
        "the chat list follows the stream",
        "api/client.py",
        [("        for _ in range(SNAPSHOT_FOLLOW):", "        for _ in range(0):")],
        ["tests/unit/test_qa_attacks.py::test_the_chat_list_reads_the_stream_past_the_sessions_start"],
    ),
    (
        "an older batch does not move a chat back",
        "tools/_common.py",
        [('if last and newest >= (entry.get("last_at") or 0, entry.get("_last_msg") or 0):', "if last:")],
        ["tests/unit/test_qa_attacks.py::test_an_older_batch_never_moves_a_chat_back"],
    ),
    (
        "an altered cursor signature is damage",
        "core/cursor.py",
        [("        if not signed:", "        if False:")],
        [
            "tests/unit/test_qa_attacks.py::test_a_cursor_with_an_altered_signature_is_damaged_and_one_from_another_list_is_foreign"
        ],
    ),
    (
        "an unread-only update stands",
        "tools/_common.py",
        [('if "unreadCnt" in d or "unread" not in entry:', 'if "unread" not in entry:')],
        ["tests/unit/test_qa_attacks.py::test_an_unread_only_update_is_not_dropped"],
    ),
    (
        "a partial chat list is not cached",
        "tools/_common.py",
        [("    if not c.vk.snapshot_partial:", "    if True:")],
        ["tests/unit/test_qa_attacks.py::test_a_failed_follow_up_batch_is_not_cached_as_the_whole_list"],
    ),
    (
        "same-second messages break ties by id",
        "tools/_common.py",
        [('key=lambda x: (x.get("time") or 0, x.get("msgId") or 0)', 'key=lambda x: x.get("time") or 0')],
        ["tests/unit/test_qa_attacks.py::test_within_one_second_the_later_message_is_the_last"],
    ),
    (
        "a network failure while following keeps the first batch",
        "api/client.py",
        [("                resp = {}\n", "                raise\n")],
        ["tests/unit/test_qa_attacks.py::test_a_network_failure_while_following_keeps_the_first_batch"],
    ),
    (
        "batches compare by time and id",
        "tools/_common.py",
        [('entry.get("_last_msg") or 0)', "0)")],
        ["tests/unit/test_qa_attacks.py::test_a_stale_batch_in_the_same_second_does_not_replace_the_later_message"],
    ),
    (
        "a stream cut at the limit is partial",
        "api/client.py",
        [("            self.snapshot_partial = bool(url)", "            pass")],
        ["tests/unit/test_qa_attacks.py::test_a_stream_longer_than_the_follow_limit_is_not_cached"],
    ),
    (
        "a resend is judged on VK's clock",
        "tools/messages.py",
        [("    asked_at = time.time() + c.vk.s.time_offset", "    asked_at = time.time()")],
        ["tests/unit/test_qa_attacks.py::test_a_fresh_send_is_not_a_resend_when_the_clocks_differ"],
    ),
    (
        "a one-to-one chat names its members",
        "tools/chats.py",
        [('"members_note": "A one-to-one chat: its members are you and them." if include_members else None,', "")],
        ["tests/unit/test_qa_attacks.py::test_members_of_a_one_to_one_chat_are_named_not_ignored"],
    ),
    (
        "an edit must be visible",
        "tools/messages.py",
        [('if action == "edit" and not _visible(text or ""):', 'if action == "edit" and not (text or "").strip():')],
        ["tests/unit/test_qa_attacks.py::test_an_edit_to_invisible_characters_is_refused"],
    ),
    (
        "button print is keyed",
        "tools/_common.py",
        [("hmac.new(key, seen, hashlib.sha256).hexdigest()[:6]", "hashlib.sha256(seen).hexdigest()[:6]")],
        ["tests/unit/test_qa_attacks.py::test_a_buttons_print_depends_on_the_servers_secret"],
    ),
    (
        "an edit turns addresses into mentions",
        "tools/messages.py",
        [("            text, _ = _inline_mentions(text.strip())", "            text = text.strip()")],
        ["tests/unit/test_qa_attacks.py::test_inline_addresses_in_an_edit_are_held_to_the_people_cap"],
    ),
    (
        "an unknown client gets a page, not JSON",
        "signin.py",
        [('if message["type"] == "http.response.start" and message["status"] == 400:', "if False:")],
        ["tests/unit/test_http.py::test_an_unknown_client_gets_a_page_that_says_to_connect_again"],
    ),
    (
        "finishing never resends a code",
        "api/login.py",
        [('"pwd": code}', '"pwd": code, "tokenType": "otp_via_email"}')],
        ["tests/unit/test_signin.py::test_finishing_sign_in_never_asks_for_another_code"],
    ),
    (
        "downloads only from the installation",
        "links/routes.py",
        [('if urlsplit(url).hostname not in link["hosts"]', "if False")],
        ["tests/unit/test_files.py::test_a_download_link_pointing_elsewhere_is_refused"],
    ),
    (
        "one message by its own id",
        "tools/_common.py",
        [('"fromMsgId": msg_id + 1, "count": -1', '"fromMsgId": msg_id, "count": 1')],
        [
            "tests/unit/test_messages.py::test_one_message_is_fetched_by_its_own_id",
            "tests/unit/test_messages.py::test_only_own_messages_change",
        ],
    ),
    (
        "a download name is never a path",
        "links/links.py",
        [('base = name.replace("\\\\", "/").rsplit("/", 1)[-1]', "base = name")],
        ["tests/unit/test_files.py::test_a_download_name_cannot_become_a_path"],
    ),
    (
        "connections are checked when made",
        "api/egress.py",
        [("if not ip.is_global or ip.is_multicast:", "if False:")],
        ["tests/unit/test_egress.py::test_a_name_resolving_to_loopback_is_refused"],
    ),
    (
        "a cursor belongs to its list",
        "core/cursor.py",
        [(' or body["q"] != (scope or {}):', ":")],
        [
            "tests/unit/test_qa_regressions.py::test_a_list_chats_cursor_is_bound_to_its_filter",
            "tests/unit/test_qa_regressions.py::test_a_history_cursor_is_bound_to_its_chat",
        ],
    ),
    (
        "only a byte range is passed upstream",
        "links/routes.py",
        [("if _RANGE.fullmatch(asked) and", "if True and")],
        ["tests/unit/test_qa_regressions.py::test_a_malformed_range_is_not_passed_on"],
    ),
    (
        "a new group gets the private defaults",
        "tools/admin.py",
        [('"about": about, "live": False, "public": False}', '"about": about}')],
        ["tests/unit/test_qa_regressions.py::test_a_new_group_is_private_and_needs_no_confirmation"],
    ),
    (
        "message ids leave as strings",
        "tools/_common.py",
        [('"id": id_text(m.get("msgId")),', '"id": m.get("msgId"),')],
        ["tests/unit/test_qa_regressions.py::test_message_ids_come_out_as_exact_strings"],
    ),
    (
        "a range past the end stays a 416",
        "links/routes.py",
        [("if upstream.status_code == 416:", "if False:")],
        ["tests/unit/test_qa_regressions.py::test_a_range_past_the_end_says_so_with_the_length"],
    ),
    (
        "a decoded body never carries the encoded length",
        "links/routes.py",
        [('headers = {"Accept-Encoding": "identity"}', "headers = {}"), ('passed.pop("content-length", None)', "pass")],
        ["tests/unit/test_qa_regressions.py::test_a_download_asks_for_identity_and_never_claims_a_wrong_length"],
    ),
    (
        "reactions leave as emoji and positive counts",
        "tools/messages.py",
        [
            (
                'if isinstance(x, dict) and isinstance(x.get("counter"), int) and x["counter"] > 0',
                "if isinstance(x, dict)",
            )
        ],
        [
            "tests/unit/test_qa_regressions.py::test_reactions_are_counts_without_ids_and_zero_counts_drop",
            "tests/unit/test_qa_live_scenarios.py::test_history_shows_reaction_counts_and_nothing_once_they_are_taken_back",
        ],
    ),
    (
        "the next history page starts at the oldest message shown",
        "tools/messages.py",
        [('c.cursor_out({"from": msgs[-1]["msgId"]}, scope)', 'c.cursor_out({"from": h.get("olderMsgId")}, scope)')],
        ["tests/unit/test_qa_live_scenarios.py::test_paging_through_history_returns_every_message_once"],
    ),
    (
        "an address before a full stop is a mention",
        "tools/messages.py",
        [("(?![\\w@-]|\\.\\w)", "(?![\\w.@-])")],
        ["tests/unit/test_qa_live_scenarios.py::test_an_address_listed_in_mentions_is_a_mention_in_the_text"],
    ),
    (
        "an attachment is a name, not a path",
        "tools/messages.py",
        [("            if not _plain_file_name(name):", "            if False:")],
        ["tests/unit/test_qa_live_scenarios.py::test_an_attachment_is_a_file_name_in_the_current_folder_not_a_path"],
    ),
    (
        "only a scheduled message that exists is cancelled",
        "tools/messages.py",
        [("            if shown not in {", "            if False and shown not in {")],
        ["tests/unit/test_qa_live_scenarios.py::test_cancelling_a_scheduled_message_that_is_not_there_is_refused"],
    ),
    (
        "mute without minutes is eternal",
        "tools/chats.py",
        [('        elif action == "mute" and minutes is None:', "        elif False:")],
        ["tests/unit/test_qa_live_scenarios.py::test_mute_without_minutes_is_an_eternal_mute"],
    ),
    (
        "a reaction is one emoji",
        "tools/messages.py",
        [('if action == "react" and not _one_emoji(reaction):', "if False:")],
        ["tests/unit/test_qa_live_scenarios.py::test_a_reaction_that_is_not_one_emoji_is_refused_before_vk"],
    ),
    (
        "a 416 names the length",
        "links/routes.py",
        [('if not unsatisfied and isinstance(link.get("size"), int):', "if False:")],
        ["tests/unit/test_qa_live_scenarios.py::test_a_range_past_the_end_names_the_length_even_when_vk_does_not"],
    ),
    (
        "curl does not glob our file names",
        "links/links.py",
        [("curl -fsS --globoff -T", "curl -fsS -T"), ("curl -fsS --globoff -o", "curl -fsS -o")],
        [
            "tests/unit/test_qa_live_scenarios.py::test_an_upload_command_turns_curl_globbing_off",
            "tests/unit/test_qa_live_scenarios.py::test_a_download_command_turns_curl_globbing_off",
        ],
    ),
    (
        "a bot's callback data never reaches the model",
        "tools/_common.py",
        [
            (
                '                "button": button_token(i, b),',
                '                "button": str(b.get("callbackData") or i),',
            )
        ],
        ["tests/unit/test_qa_attacks.py::test_a_bots_callback_data_cannot_forge_a_marker"],
    ),
    (
        "a pressed button sends the bot's own data",
        "tools/bots.py",
        [('            data = keys[n - 1].get("callbackData")', "            data = button")],
        ["tests/unit/test_qa_attacks.py::test_a_forged_callback_still_presses_with_the_original_data"],
    ),
    (
        "a message id fits in 64 bits",
        "tools/_common.py",
        [("    if n > MAX_MSG_ID:", "    if False:")],
        ["tests/unit/test_qa_attacks.py::test_a_message_id_beyond_64_bits_is_refused_before_vk"],
    ),
    (
        "a poll answer is not empty",
        "tools/messages.py",
        [("        if not all(_visible(o) for o in poll_options):", "        if False:")],
        ["tests/unit/test_qa_attacks.py::test_a_poll_with_an_empty_answer_is_refused_at_save"],
    ),
    (
        "a send time is at most a year ahead",
        "tools/messages.py",
        [("        if when > time.time() + MAX_SCHEDULE_DAYS * 86400:", "        if False:")],
        ["tests/unit/test_qa_attacks.py::test_a_send_time_decades_ahead_is_refused_at_save"],
    ),
    (
        "mentions are held to the people cap",
        "tools/messages.py",
        [
            ("    if len(notified) > MAX_PEOPLE:", "    if False:"),
            ("    if len(mention or []) > MAX_PEOPLE:", "    if False:"),
        ],
        [
            "tests/unit/test_qa_attacks.py::test_mentions_are_held_to_the_people_cap",
            "tests/unit/test_qa_attacks.py::test_inline_addresses_are_held_to_the_people_cap",
            "tests/unit/test_qa_attacks.py::test_native_mention_syntax_is_held_to_the_people_cap",
        ],
    ),
    (
        "an edit is held to the text limit",
        "tools/messages.py",
        [('        if action == "edit" and len(text.strip()) > MAX_TEXT:', "        if False:")],
        ["tests/unit/test_qa_attacks.py::test_an_edit_is_held_to_the_text_limit"],
    ),
    (
        "two poll answers are not the same",
        "tools/messages.py",
        [("        if len({o.casefold() for o in poll_options}) != len(poll_options):", "        if False:")],
        ["tests/unit/test_qa_attacks.py::test_a_poll_with_the_same_answer_twice_is_refused_at_save"],
    ),
    (
        "an edit is held to the people cap",
        "tools/messages.py",
        [
            (
                '        if action == "edit" and len(_native_mentions(text)) > MAX_PEOPLE:',
                "        if False:",
            )
        ],
        ["tests/unit/test_qa_attacks.py::test_an_edit_is_held_to_the_people_cap"],
    ),
    (
        "a changed keyboard is not pressed",
        "tools/bots.py",
        [("if not 1 <= n <= len(keys) or button_token(n, keys[n - 1]) != button:", "if not 1 <= n <= len(keys):")],
        ["tests/unit/test_qa_attacks.py::test_a_keyboard_the_bot_changed_after_it_was_read_is_not_pressed"],
    ),
    (
        "an invisible poll answer is empty",
        "tools/messages.py",
        [("        if not all(_visible(o) for o in poll_options):", "        if not all(poll_options):")],
        ["tests/unit/test_qa_attacks.py::test_a_poll_answer_of_invisible_characters_is_refused"],
    ),
    (
        "VK Teams down is not a refused token",
        "api/client.py",
        [
            (
                'raise VKError("failed_read" if isinstance(code, int) and code >= 500 else "reconnect")',
                'raise VKError("reconnect")',
            )
        ],
        ["tests/unit/test_replicas.py::test_a_disconnect_while_vk_teams_is_down_is_reported"],
    ),
    (
        "save_draft refuses what it cannot seal",
        "tools/messages.py",
        [("    except TooLarge:\n", "    except ZeroDivisionError:\n")],
        ["tests/unit/test_replicas.py::test_save_draft_refuses_a_draft_too_large_to_seal"],
    ),
    (
        "no aimsid in logs",
        "api/client.py",
        [('logging.getLogger("httpx").setLevel(logging.WARNING)', "pass")],
        ["tests/unit/test_gates.py::test_a_full_message_flow_logs_no_content"],
    ),
    (
        "a sealed blob is bound to its kind",
        "core/seal.py",
        [
            ("self._aead.decrypt(raw[:12], raw[12:], kind.encode())", "self._aead.decrypt(raw[:12], raw[12:], None)"),
            (
                "self._aead.encrypt(nonce, zlib.compress(body, 9), kind.encode())",
                "self._aead.encrypt(nonce, zlib.compress(body, 9), None)",
            ),
        ],
        ["tests/unit/test_oauth.py::test_a_blob_of_one_kind_is_not_another"],
    ),
    (
        "a sealed blob expires",
        "core/seal.py",
        [('or body["exp"] < self._clock():', ":")],
        [
            "tests/unit/test_oauth.py::test_tokens_expire",
            "tests/unit/test_oauth.py::test_a_code_and_a_sign_in_request_expire",
        ],
    ),
    (
        "a code belongs to its client",
        "oauth/provider.py",
        [
            (
                'if p is None or p["client_id"] != client.client_id or authorization_code in self._used_codes:',
                "if p is None or authorization_code in self._used_codes:",
            )
        ],
        ["tests/unit/test_oauth.py::test_a_code_belongs_to_its_client"],
    ),
    (
        "a code is used once",
        "oauth/provider.py",
        [(" or authorization_code in self._used_codes:", ":")],
        ["tests/unit/test_oauth.py::test_a_code_is_used_once"],
    ),
    (
        "a refresh token belongs to its client",
        "oauth/provider.py",
        [
            (
                '        if p is None or p["client_id"] != client.client_id:\n            return None\n'
                "        return RefreshToken(",
                "        if p is None:\n            return None\n        return RefreshToken(",
            )
        ],
        ["tests/unit/test_oauth.py::test_a_refresh_token_belongs_to_its_client"],
    ),
    (
        "revoking ends the VK Teams session",
        "oauth/provider.py",
        [('            await self._end_session(VKSession.from_json(p["s"]))', "            pass")],
        ["tests/unit/test_oauth.py::test_revoking_a_token_ends_the_vk_teams_session"],
    ),
    (
        "a draft belongs to its person and chat",
        "tools/messages.py",
        [('if saved is None or saved.get("p") != c.person or saved.get("chat") != sn:', "if saved is None:")],
        ["tests/unit/test_messages.py::test_send_draft_without_a_saved_draft"],
    ),
    (
        "files before send",
        "tools/messages.py",
        [
            (
                'if got is None or sorted(f["uid"] for f in got) != sorted(wanted) '
                'or any(f["chat"] != sn for f in got):',
                "if False:",
            )
        ],
        ["tests/unit/test_messages.py::test_send_draft_waits_for_announced_files"],
    ),
    (
        "a resend keeps its localMsgId",
        "tools/messages.py",
        [
            (
                'result = await _send(c, sn, parts, when, ids.get("main"))',
                "result = await _send(c, sn, parts, when, None)",
            )
        ],
        [
            "tests/unit/test_messages.py::test_send_draft_sends_exactly_the_saved_text_once",
            "tests/unit/test_messages.py::test_two_overlapping_sends_post_the_same_message",
        ],
    ),
    (
        "a scheduled resend is not queued twice",
        "tools/messages.py",
        [("        queued = await _already_scheduled(c, sn, parts, when)", "        queued = None")],
        ["tests/unit/test_messages.py::test_a_scheduled_draft_sent_again_is_not_queued_twice"],
    ),
    (
        "a lost upload response can be uploaded again",
        "links/routes.py",
        [('        rt.links.release_upload(link["uid"])', "        pass")],
        ["tests/unit/test_files.py::test_an_upload_lands_in_vk_teams_and_send_draft_posts_it"],
    ),
    (
        "a blob too large to open is never made",
        "core/seal.py",
        [("        if len(blob) > MAX_BLOB:", "        if False:")],
        ["tests/unit/test_replicas.py::test_a_blob_too_large_to_open_is_never_made"],
    ),
    (
        "a poll answer has a length limit",
        "tools/messages.py",
        [("        if any(len(o) > MAX_POLL_OPTION for o in poll_options):", "        if False:")],
        ["tests/unit/test_replicas.py::test_a_long_poll_answer_is_refused_at_save"],
    ),
    (
        "only plain text is matched against the queue",
        "tools/messages.py",
        [
            (
                "queued = await _already_scheduled(c, sn, parts, when) if plain else None",
                "queued = await _already_scheduled(c, sn, parts, when)",
            )
        ],
        ["tests/unit/test_replicas.py::test_a_scheduled_poll_is_not_mistaken_for_queued_text"],
    ),
    (
        "a failed disconnect is reported",
        "api/client.py",
        [('            if e.key != "reconnect":', "            if False:")],
        ["tests/unit/test_replicas.py::test_a_failed_disconnect_is_reported_not_swallowed"],
    ),
    (
        "a full code cache keeps recording",
        "oauth/provider.py",
        [
            ("        while len(self._used_codes) >= USED_CODES_MAX:", "        while False:"),
            (
                "        self._used_codes[authorization_code.code] = authorization_code.expires_at",
                "        if len(self._used_codes) < USED_CODES_MAX:\n"
                "            self._used_codes[authorization_code.code] = authorization_code.expires_at",
            ),
        ],
        ["tests/unit/test_replicas.py::test_a_full_code_cache_still_refuses_the_newest_replay"],
    ),
]


def run(tests: list[str]) -> int:
    """pytest's exit code: 0 passed, 1 a test failed; anything else (import or collection errors, no tests)
    is not evidence that a test caught the mutation."""
    return subprocess.run(  # noqa: S603 - fixed argv: this interpreter, pytest, test ids from this file
        [sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", *tests], cwd=ROOT, capture_output=True
    ).returncode


def main() -> int:
    assert len(MUTATIONS) >= 15, "too few mutations: the check is broken"
    if run([t for m in MUTATIONS for t in m[3]]) != 0:
        print("BASELINE FAILS: the named tests must pass before any mutation")
        return 2
    failed = []
    for what, rel, edits, tests in MUTATIONS:
        f = SRC / rel
        original = f.read_text()
        mutated = original
        for old, new in edits:
            if old not in mutated:
                failed.append(f"{what}: guard text not found in {rel} (refactored? update the mutation)")
                break
            mutated = mutated.replace(old, new, 1)
        else:
            f.write_text(mutated)
            try:
                codes = {t: run([t]) for t in tests}
            finally:
                f.write_text(original)
            survived = [t for t, code in codes.items() if code == 0]
            broken = [f"{t} (exit {code})" for t, code in codes.items() if code not in (0, 1)]
            if survived or broken:
                failed.append(
                    f"{what}: "
                    + "; ".join(
                        ([f"still passes without its guard: {', '.join(survived)}"] if survived else [])
                        + ([f"did not run cleanly, so proves nothing: {', '.join(broken)}"] if broken else [])
                    )
                )
                continue
            print(f"ok   {what}")
            continue
    for line in failed:
        print(f"FAIL {line}")
    print(f"{len(MUTATIONS) - len(failed)}/{len(MUTATIONS)} guards are held by a test that fails without them")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
