# VK Teams / VK WorkSpace client API: cheat sheet (verified live, api v139)

Source of truth: `info.x-live-findings` and per-operation `x-live-probe` / `x-live-test` in the OpenAPI file.
Re-verify: `uv run python tests/live/legacy_login.py --host <host>` → `uv run pytest tests/live`. The suites that check
answers against an OpenAPI description (methods, features) need it in `VKTEAMS_SPEC` and skip without it;
the sign-in and end-to-end checks do not.

## 1. Bootstrap

| # | Call | Auth | Result |
|---|---|---|---|
| 1 | `GET /myteam-config.json` | none | `api-version` (→ `N`), `api-urls.main-api`, feature flags |
| 2 | `POST /api/vN/wim/auth/clientLogin` `f=json devId s=<email> pwd=none tokenType=otp_via_email` | none | 200, **empty** `token.a` → code e-mailed |
| 3 | `POST /api/vN/wim/auth/clientLogin` `f=json devId s=<email> pwd=<CODE>` | none | `token.a`, `sessionSecret`, `hostTime` |
| 4 | `POST /api/vN/wim/aim/startSession` `a k ts f=json events view … sig_sha256` | signature | `aimsid`, `fetchBaseURL`, `myInfo` |
| 5 | `GET <fetchBaseURL>&f=json&timeout=…` | aimsid in URL | events + **next** `fetchBaseURL` |

Signature:

| Value | Formula |
|---|---|
| `sessionKey` | `b64(HMAC-SHA256(key=CODE, msg=sessionSecret))` |
| `sig_sha256` | `b64(HMAC-SHA256(key=sessionKey, msg="POST&"+pct(url)+"&"+pct(sorted "k=pct(v)" joined by &)))` |

## 2. Calling

| Family | URL | aimsid | Success | Failure |
|---|---|---|---|---|
| rapi | `POST /api/vN/rapi/<method>` JSON `{reqId, aimsid, params}` | JSON body **only** | `status.code` 200xx | 40000 bad request, 40001 refused, 40200 bad token, 50000 server |
| wim | `GET/POST /api/vN/wim/<path>?f=json&aimsid=` | query / form | `response.statusCode` 200 | 401 inside HTTP 200 |

## 3. Traps

| Trap | Symptom | Fix |
|---|---|---|
| Spec prefix `/api/v/rapi` | 40000 Unknown method, every method | `/api/v<api-version>/rapi` |
| Code sent with `tokenType=otp_via_email` | 200 empty token, new e-mail, old code dead | submit code without tokenType |
| `devId` missing | 460 Missing required parameter (k) | public dev id from desktop binary |
| aimsid in query/header for rapi | 40200 | put it in JSON body |
| Old `fetchBaseURL` reused | events replayed | persist the one from each response |
| `sendIM` `msgId` | UUID, not usable for delete | use `histMsgId` |
| `rapi/message/send` | DLP error | `wim/im/sendIM` (`message-send-api-enabled=false`) |
| Personal sn in chat methods | Invalid chatId / Bad sn | `<n>@chat.agent`, chatType group |
| `filter` as string | Jimny error | `filter: {keyword}` |
| `getHistory` without patchVersion | Required parameter not found | `patchVersion: "init"` |
| `folders/create` | `type` rejected; title > 15 → 40001 | `{title≤15, chats:[]}` |
| `reactions/v2/*` | Unknown method | `reaction/*` |
| Unknown required fields | — | call with bogus aimsid: server names them before auth |

## 4. Working examples

| Method | params |
|---|---|
| `getHistory` | `{sn, fromMsgId:-1, count:-20, patchVersion:"init"}` |
| `getHistoryBatch` | `{sn, patchVersion:"init", subreqs:[{fromMsgId:-1, count:-2, subreqId:"1"}]}` |
| `getChatInfo` | `{sn:<chat>, memberLimit:5}` |
| `getChatMembers` | `{id:<chat>, pageSize:5}` |
| `searchAllDialogs` | `{pagesize:5, filter:{keyword:"…"}}` |
| `searchChatThreads` | `{sn:<chat>, filter:{keyword:"…"}}` |
| `getMsgGallery` | `{sn:<chat>, subreqs:[{subreqId:"1", urlType:"image", fromMsgId:-1, msgCount:-5}]}` |
| `message/scheduled/history/get` | `{sn, historyVersion:"0"}` |
| `wim/im/sendIM` | form `t=<sn> message=<text>` |
| `delMsgBatch` | `{sn, msgIds:[histMsgId], shared:false}` |
| `reaction/add` / `remove` | `{msgId, chatId, reaction, reactions:[…]}` / `{msgId, chatId}` |
| `draft/set` | `{sn, time, parts:[{mediaType:"text", text}]}`; clear with `parts:[]` |
| `folders/create` → `delete` | `{title, chats:[]}` → `results.folderId` → `{folderId}` |

## 5. Features (all verified live)

| Feature | How |
|---|---|
| Upload file | `GET {base}/files/init?aimsid&f=json&k&size&filename&client&r` → `result.host,url` → `POST https://host+url?aimsid&f=json&k&r` body=bytes, headers `Session-ID`, `Content-Range: bytes 0-(n-1)/n`, `Content-Disposition`, octet-stream → `result.fileid, static_url` |
| File info / download | `GET {base}/files/info/<fileid>/?f=json&aimsid` → `status 20000`, `result.info.dlink` (signed) → GET bytes |
| Send file | `sendIM message=<static_url>` |
| Create group | `createChat {name, aimSid, about, members:[{sn}]}` → `results.sn` |
| Edit group | `modChat {sn, name, about, rules, threadsEnabled, public, joinModeration}` |
| Leave group | wim `buddylist/removeBuddy?buddy=<sn>&allGroups=1` (no delete exists) |
| Members | `group/members/add {sn, members:[sn]}`, `…/delete`, `modChatMember {sn, memberSn, role}`, block/unblock `members:[{sn}]` |
| Member info | `getChatMemberInfo {id, filter:{members:[sn]}}` |
| Mention | text `@[<sn>]` + form `mentions=<sn>,<sn>` |
| Edit message | `sendIM updateMsgId=<histMsgId>` |
| Pin | `pinMessage {sn, msgId[, unpin:true]}` |
| Quote / forward | `sendIM parts=[{mediaType:"quote"\|"forward", text, sn, msgId, time, chat:{sn}}, {mediaType:"text", text}]` |
| Poll | `parts=[{mediaType:"text", text, poll:{type:"anon", responses:[…]}}]` → `poll/get {id, subscribe}` → `poll/vote {id, answerId}` / `revoke` / `stop` |
| Reactions | `reaction/add {msgId, chatId, reaction, reactions:[set]}`, `get {chatId, msgIds}`, `list {…, reaction, newerThan, limit}`, `remove` |
| Thread | `thread/add {parentTopic:{type:"message", chatId, messageId}}` → `threadId` (a chat sn) → `sendIM t=<threadId>` |
| Call link | `conference/create {name, type:"equitable"\|"webinar", participants:[], callParticipants:false}` → `conferenceUrl, conferencePin` |
| Bot | `startBot {bot:"70001", startParameter:""}` → reply with `inlineKeyboardMarkup` → `getBotCallbackAnswer {chatId, msgId, callbackData}` → 20080 |
| Typing / read / mute | wim `im/setTyping?t&typingStatus=typing\|none`; `setDlgState {sn, lastRead}`; wim `buddylist/Mute?buddy&eternal=1\|0`; `contacts/mute {sn, duration, tieCounter}` |
| Folders / archive | `folders/create {title, chats}`, `chatPin`, `chatsModify {addChats\|delChats}`; `archive/chatsModify` |
| Avatar | `GET <ub host>/api/vN/files/avatar/get?aimsid&targetSn&size=128\|256\|1024` |
| Tasks | `POST /api/vN/tasks/list` (not rapi) |

## 6. Result codes

| Code | Meaning |
|---|---|
| 20000 / 20001 | OK / OK-empty |
| 20070 | partial failure: read `results.failures[{id,error}]` |
| 20080 | accepted, answer async |
| 40000 / 40001 / 40200 / 40400 | bad request / refused / bad token / bad target |
| 50000 | server error or timeout |

## 7. Sending via rapi (verified live)

| What | params |
|---|---|
| Text | `message/send {target, localMsgId:<uuid>, parts:{mainPart:{text:{plain}}}}` → `results.msgId` |
| Reply | `parts:{mainPart:{text:{plain}}, quoteParts:[{sn, msgId, time, chat:{sn}, text:{plain}}]}` |
| Edit | same + `updateMsgId` |
| Scheduled | same + `schedule:{scheduledTime:<unix s>}` → **20020**, `results.scheduled.scheduledMsgId` |
| Scheduled list / cancel / send now | `message/scheduled/history/get {sn, historyVersion:"0"}` → `messages.new[]`; `…/cancel {sn, scheduledMsgIds}`; `…/flush {sn, scheduledMsgIds}` |
| Trap | wrong `parts` shape → `40000 DLP error - Invalid request` (the shape is wrong; the same text in the right shape goes through) |

## 8. Open

| What | Why |
|---|---|
| Group members add / roles / block on a real member | needs a 2nd account; Metabot refuses (`bot_setjoingroups_false`) |
| `profile/update` | changes your visible name; server flags forbid self name change; not run |
| `status/set`, `setNick`, `session/reset*`, `reportAbuse`, `misc/support` | visible to others or destructive; not run |
| `misc/preview/getURLContent` | 500 for external URLs (closed perimeter) |
| okcalls/*, reactions/v2/*, rapi tasks/* | not routed on this server version |
