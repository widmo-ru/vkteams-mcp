"""Interactive login by e-mailed one-time code: you type the code, it never leaves this process except to the server.

    uv run python tests/live/legacy_login.py --host u.vkteams.example.com --login you@company.com

Writes ~/.config/vkteams-mcp/session.json (mode 600): aimsid, token `a`, session key.
The code itself is not stored.
"""

from __future__ import annotations

import argparse
import getpass
import secrets
import sys
import time

from vkclient import DEV_IDS, Client, Session, derive_session_key, discover


def _redact(d):
    if isinstance(d, dict):
        return {k: ("<redacted>" if k in {"a", "aimsid", "sessionSecret", "token", "fetchBaseURL"} else _redact(v))
                for k, v in d.items()}
    if isinstance(d, list):
        return [_redact(x) for x in d]
    return d


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True, help="messenger host, e.g. u.vkteams.example.com")
    ap.add_argument("--login")
    ap.add_argument("--code-only", action="store_true", help="do not request a new code; use the one already e-mailed")
    a = ap.parse_args()

    a.login = a.login or input("Логин (корпоративная почта): ").strip()
    cfg = discover(a.host)
    sess = Session(host=a.host, api_version=int(cfg["api-version"]), dev_id=DEV_IDS["desktop"], login=a.login)
    c = Client(sess)
    print(f"server api-version={sess.api_version}; base={c.base}")

    # Step 1: ask the server to e-mail a one-time code. pwd must be present; any non-code value triggers the e-mail.
    if not a.code_only:
        req = c.client_login(a.login, secrets.token_urlsafe(12), {"tokenType": "otp_via_email"})
        r = req.get("response", {})
        if r.get("statusCode") != 200:
            print("Запрос кода отказан:", _redact(req))
            return 1
        print(f"Код отправлен на {a.login}. Проверь почту.")

    # Step 2: submit the code. NEVER with tokenType=otp_via_email here: the server treats that as
    # "send a new code" and the one just typed is invalidated (observed). Variants below get 330 on a
    # wrong code without sending mail, so retyping is safe.
    r, code = {}, ""
    for attempt in range(3):
        code = getpass.getpass("Код из письма: ").strip().upper()
        for variant in ({}, {"tokenType": "longterm"}):
            resp = c.client_login(a.login, code, variant)
            r = resp.get("response", {})
            tok = r.get("data", {}).get("token", {}).get("a")
            print(f"  {variant.get('tokenType', 'без tokenType')}: {r.get('statusCode')} {r.get('statusText')}"
                  f"{' — токен получен' if tok else ''}")
            if r.get("statusCode") == 200 and tok:
                break
        else:
            print("  Код не принят. Проверь его в последнем письме и введи ещё раз (новое письмо НЕ отправлялось).")
            continue
        break
    else:
        print("Код так и не принят. Запусти без --code-only, чтобы получить новый.")
        return 1
    data = r["data"]
    sess.token_a = data["token"]["a"]
    sess.session_key = derive_session_key(code, data["sessionSecret"])
    sess.time_offset = int(data.get("hostTime", time.time())) - int(time.time())
    sess.my_sn = data.get("loginId", a.login)
    del code
    sess.save()  # keep the token even if startSession fails, so it can be retried without a new code
    print("clientLogin OK, token expiresIn:", data["token"].get("expiresIn"))

    ss = c.start_session()
    r = ss.get("response", {})
    if r.get("statusCode") != 200:
        print("startSession отказал:", _redact(ss))
        return 1
    d = r["data"]
    sess.aimsid = d["aimsid"]
    sess.fetch_base_url = d.get("fetchBaseURL", "")
    sess.my_sn = d.get("myInfo", {}).get("aimId", sess.my_sn)
    sess.save()
    print(f"startSession OK, я = {sess.my_sn}. Сессия сохранена (600).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
