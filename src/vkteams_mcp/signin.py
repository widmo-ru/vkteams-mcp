"""The sign-in page an MCP client opens in the person's browser.

Step 1 asks the VK Teams server address and the person's e-mail; the server proves the address is a
VK Teams installation (`api.install`) and asks VK Teams to e-mail a code. Step 2 takes the code,
signs in (`api.login`) and hands the session to the OAuth provider, which redirects the browser
back to the client with a one-time authorization code. The code from the e-mail exists only in this
request; the model never sees it.

The page loads nothing from outside this server (a closed perimeter has no CDN): fonts and styles
are served from /static, and a strict Content-Security-Policy says so to the browser.
"""

from __future__ import annotations

import html
import time
from dataclasses import dataclass
from pathlib import Path

from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, RedirectResponse, Response

from vkteams_mcp.api.install import Installation, discover, system_resolve
from vkteams_mcp.api.login import CodeLimiter, finish_sign_in, request_code
from vkteams_mcp.core.context import runtime
from vkteams_mcp.core.errors import VKError

PENDING_TTL = 15 * 60
MAX_CODE_TRIES = 5
STATIC = Path(__file__).resolve().parent / "static"
_STATIC_TYPES = {".woff2": "font/woff2", ".css": "text/css; charset=utf-8", ".txt": "text/plain; charset=utf-8"}


@dataclass
class Pending:
    installation: Installation
    address: str
    expires: float
    tries: int = 0


class SignIn:
    def __init__(self, provider) -> None:
        self.provider = provider
        self.limiter = CodeLimiter()
        self._pending: dict[str, Pending] = {}

    async def page(self, request: Request) -> Response:
        lang = pick_lang(request.headers.get("accept-language", ""))
        rid = request.query_params.get("request", "")
        if await self.provider.sign_in_context(rid) is None:
            return _page(lang, error=T[lang]["expired"], status=410)
        return _page(lang, step=1, form=_step1(lang, rid, fixed=self._fixed()))

    def _fixed(self) -> str | None:
        """The one installation this connector is pinned to, if it is pinned to exactly one."""
        pinned = runtime().settings.policy.installations
        return next(iter(pinned)) if len(pinned) == 1 else None

    async def submit(self, request: Request) -> Response:
        lang = pick_lang(request.headers.get("accept-language", ""))
        form = await request.form()
        rid = str(form.get("request", ""))
        ctx = await self.provider.sign_in_context(rid)
        if ctx is None:
            return _page(lang, error=T[lang]["expired"], status=410)
        if form.get("step") == "code":
            return await self._code(lang, rid, str(form.get("code", "")), ctx)
        server = self._fixed() or str(form.get("server", ""))
        return await self._address(lang, request, rid, server, str(form.get("email", "")), ctx)

    async def _address(self, lang: str, request: Request, rid: str, server: str, email: str, ctx: dict) -> Response:
        rt = runtime()
        self._pending.pop(rid, None)  # a restarted address step invalidates the previous code step at once
        email = email.strip()
        again = _step1(lang, rid, server, email, self._fixed())
        if "@" not in email or len(email) > 254:
            return _page(lang, step=1, form=again, error=T[lang]["bad_email"], status=400)
        try:
            self.limiter.take(address=email, network=request.client.host if request.client else None)
            inst = await discover(server, rt.http, rt.settings.policy, resolve=rt.resolve or system_resolve)
            await request_code(inst, email, rt.http, dev_id=rt.settings.dev_id)
        except VKError as e:
            return _page(lang, step=1, form=again, error=translate(lang, e.text), status=400)
        self._sweep()
        self._pending[rid] = Pending(inst, email, time.time() + PENDING_TTL)
        return _page(lang, step=2, form=_step2(lang, rid, email), redirect=ctx["redirect_origin"])

    async def _code(self, lang: str, rid: str, code: str, ctx: dict) -> Response:
        rt = runtime()
        t = T[lang]
        p = self._pending.get(rid)
        if p is None or p.expires < time.time():
            return _page(lang, step=1, form=_step1(lang, rid, fixed=self._fixed()), error=t["code_expired"], status=400)
        p.tries += 1
        if p.tries > MAX_CODE_TRIES:
            self._pending.pop(rid, None)
            return _page(lang, error=t["too_many_codes"], status=429)
        try:
            session = await finish_sign_in(p.installation, p.address, code, rt.http, dev_id=rt.settings.dev_id)
        except VKError as e:
            return _page(
                lang,
                step=2,
                form=_step2(lang, rid, p.address),
                redirect=ctx["redirect_origin"],
                error=translate(lang, e.text),
                status=400,
            )
        if self._pending.get(rid) is not p:
            # the address step was submitted again while this code was checked: the newer one wins
            return _page(lang, error=t["restarted"], status=409)
        self._pending.pop(rid, None)
        try:
            redirect = await self.provider.complete_sign_in(rid, session)
        except LookupError:
            return _page(lang, error=t["expired"], status=410)
        return RedirectResponse(redirect, status_code=302)

    def _sweep(self) -> None:
        now = time.time()
        for k in [k for k, v in self._pending.items() if v.expires < now]:
            del self._pending[k]


async def static(request: Request) -> Response:
    """Fonts and their licences, from the package. Nothing outside `static/` is reachable."""
    rel = request.path_params.get("path", "")
    target = (STATIC / rel).resolve()
    if not target.is_relative_to(STATIC) or not target.is_file() or target.suffix not in _STATIC_TYPES:
        return Response("Not found", status_code=404)
    return FileResponse(
        target, media_type=_STATIC_TYPES[target.suffix], headers={"Cache-Control": "public, max-age=604800, immutable"}
    )


# ---------------------------------------------------------------- language

T: dict[str, dict[str, str]] = {
    "en": {
        "title": "Sign in to VK Teams",
        "description": "Sign in to VK Teams to connect it to your AI app.",
        "step": "Step {n} of 2",
        "h1": "Sign in to VK Teams",
        "h1_code": "Enter the code",
        "server": "VK Teams server",
        "server_hint": "The address you open VK Teams at.",
        "server_placeholder": "teams.example.com",
        "email": "Work e-mail",
        "email_placeholder": "you@example.com",
        "send": "Get a code",
        "note": "Messages go out only after you confirm them.",
        "sent": "VK Teams sent a code to <b>{email}</b>.",
        "code": "Code",
        "connect": "Connect",
        "other": "Use another address",
        "expired": "This sign-in link has expired or was already used. Connect again from your AI app.",
        "bad_email": "Type your work e-mail address.",
        "code_expired": "The code step has expired. Start again.",
        "too_many_codes": "Too many wrong codes. Connect again from your AI app.",
        "restarted": "The sign-in was restarted in another tab. Use the latest code.",
    },
    "ru": {
        "title": "Вход в VK Teams",
        "description": "Войдите в VK Teams, чтобы подключить его к своему ИИ-приложению.",
        "step": "Шаг {n} из 2",
        "h1": "Вход в VK Teams",
        "h1_code": "Введите код",
        "server": "Сервер VK Teams",
        "server_hint": "Адрес, по которому вы открываете VK Teams.",
        "server_placeholder": "teams.example.ru",
        "email": "Рабочая почта",
        "email_placeholder": "you@example.ru",
        "send": "Получить код",
        "note": "Сообщения уходят только после вашего подтверждения.",
        "sent": "VK Teams отправил код на <b>{email}</b>.",
        "code": "Код",
        "connect": "Подключить",
        "other": "Другой адрес",
        "expired": "Ссылка для входа устарела или уже использована. Подключитесь заново из ИИ-приложения.",
        "bad_email": "Введите рабочую почту.",
        "code_expired": "Время ввода кода истекло. Начните заново.",
        "too_many_codes": "Слишком много неверных кодов. Подключитесь заново из ИИ-приложения.",
        "restarted": "Вход начали заново в другой вкладке. Введите последний код.",
    },
}

# Refusals raised below this page (api.install, api.login) are English sentences; the page shows them
# in Russian when it speaks Russian. An unknown sentence stays English rather than disappearing.
_REFUSE_TAIL = " Type the address of your VK Teams server, for example the host you open VK Teams on."
_RU_ERRORS = {
    "That is not a server address.": "Это не адрес сервера.",
    "Only https addresses are accepted.": "Подходят только адреса https.",
    "Give the address without a port.": "Укажите адрес без порта.",
    "Give a name, not an IP address.": "Укажите имя, а не IP-адрес.",
    "That name does not resolve.": "Такое имя не находится в DNS.",
    "That name points to a private network, which this server does not reach.": (
        "Это имя ведёт во внутреннюю сеть, а этот сервер туда не ходит."
    ),
    "The server's configuration does not name an https API address.": "В настройках сервера нет адреса API по https.",
    "This connector serves only the VK Teams installations its operator listed.": (
        "Этот коннектор работает только с серверами VK Teams, которые указал его администратор."
    ),
    "That address is not a VK Teams server.": "По этому адресу нет сервера VK Teams.",
    "That server did not answer.": "Сервер не ответил.",
    "VK Teams did not send a code. Check the address and the server, then try again.": (
        "VK Teams не отправил код. Проверьте почту и сервер и попробуйте снова."
    ),
    "Type the code from the e-mail.": "Введите код из письма.",
    "That code was not accepted. Check it against the latest e-mail and type it again; no new code was sent.": (
        "Код не подошёл. Сверьте его с последним письмом и введите снова. Новый код не отправлялся."
    ),
    "Too many codes were requested. Use the code already sent, or try again in an hour.": (
        "Слишком много запросов кода. Введите уже присланный код или попробуйте через час."
    ),
}
_RU_REFUSE_TAIL = " Введите адрес своего сервера VK Teams, например тот, на котором вы открываете VK Teams."


def pick_lang(accept: str) -> str:
    """The first of ru and en the browser asks for, by quality; English when it names neither."""
    best, best_q = "en", -1.0
    for part in accept.split(",")[:20]:
        tag, _, params = part.strip().partition(";")
        base = tag.strip().lower().split("-")[0]
        if base not in T:
            continue
        q = 1.0
        if params.strip().startswith("q="):
            try:
                q = float(params.strip()[2:])
            except ValueError:
                q = 0.0
        if q > 0 and q > best_q:
            best, best_q = base, q
    return best


def translate(lang: str, text: str) -> str:
    if lang != "ru":
        return text
    if text.endswith(_REFUSE_TAIL):
        why = text[: -len(_REFUSE_TAIL)]
        return _RU_ERRORS.get(why, why) + _RU_REFUSE_TAIL
    return _RU_ERRORS.get(text, text)


# ---------------------------------------------------------------- HTML


def _step1(lang: str, rid: str, server: str = "", email: str = "", fixed: str | None = None) -> str:
    t = T[lang]
    where = (
        f'<p class="fixed">{t["server"]} <b>{html.escape(fixed)}</b></p>'
        if fixed
        else f"""
    <label for="server">{t["server"]}</label>
    <input id="server" name="server" value="{html.escape(server)}" placeholder="{t["server_placeholder"]}"
           autocomplete="url" spellcheck="false" autocapitalize="off" aria-describedby="server-hint" required>
    <p class="hint" id="server-hint">{t["server_hint"]}</p>"""
    )
    return f"""
  <form method="post" action="/signin" novalidate>
    <input type="hidden" name="request" value="{html.escape(rid)}">
    <input type="hidden" name="step" value="address">{where}
    <label for="email">{t["email"]}</label>
    <input id="email" name="email" type="email" value="{html.escape(email)}" placeholder="{t["email_placeholder"]}"
           autocomplete="email" spellcheck="false" autocapitalize="off" required autofocus>
    <button type="submit">{t["send"]}</button>
  </form>
  <p class="note">{t["note"]}</p>"""


def _step2(lang: str, rid: str, email: str) -> str:
    t = T[lang]
    return f"""
  <p class="lede">{t["sent"].format(email=html.escape(email))}</p>
  <form method="post" action="/signin" novalidate>
    <input type="hidden" name="request" value="{html.escape(rid)}">
    <input type="hidden" name="step" value="code">
    <label for="code">{t["code"]}</label>
    <input id="code" class="code" name="code" autocomplete="one-time-code" inputmode="text" maxlength="12"
           spellcheck="false" autocapitalize="characters" required autofocus>
    <button type="submit">{t["connect"]}</button>
  </form>
  <p class="again"><a href="/signin?request={html.escape(rid)}">{t["other"]}</a></p>"""


_STYLE = """
:root{
--paper:#f5f5f4;--surface:#ffffff;--ink:#0c0c0d;--ink-2:#2c2c2e;--muted:#5f5f63;--line:#e3e3e4;--line-2:#cfcfd1;
--accent:#f95801;--accent-ink:#b83e00;--on-ink:#fafaf9;--error-bg:#fff1e8;--error-ink:#8a2f00;--dot:rgba(12,12,13,.10);
--focus:#f95801;
--display:'Space Grotesk',ui-sans-serif,system-ui,sans-serif;
--body:'Instrument Sans',ui-sans-serif,system-ui,-apple-system,'Segoe UI',sans-serif;
--mono:'JetBrains Mono',ui-monospace,SFMono-Regular,Menlo,monospace;
--space-1:4px;--space-2:8px;--space-3:12px;--space-4:16px;--space-5:20px;--space-6:24px;--space-8:32px;--space-10:40px;
--r-card:20px;--r-field:10px;--ease:cubic-bezier(.2,.7,.2,1);color-scheme:light}
*{box-sizing:border-box}
html,body{margin:0;min-height:100%;overflow-x:clip}
body{background-color:var(--paper);background-image:radial-gradient(var(--dot) 1px,transparent 1.2px);
background-size:24px 24px;color:var(--ink);font:16px/1.55 var(--body);-webkit-font-smoothing:antialiased}
.wrap{max-width:440px;margin:0 auto;display:grid;grid-template-columns:minmax(0,1fr);gap:var(--space-6);
padding:max(var(--space-8),env(safe-area-inset-top)) max(var(--space-4),env(safe-area-inset-right)) var(--space-10)
max(var(--space-4),env(safe-area-inset-left))}
.card{background:var(--surface);border:1px solid var(--line);border-radius:var(--r-card);padding:var(--space-8);
display:grid;gap:var(--space-4)}
.bar{display:grid;grid-template-columns:1fr 1fr;gap:var(--space-1)}
.bar span{height:4px;border-radius:2px;background:var(--line)}
.bar span.on{background:var(--accent)}
h1{font:600 32px/1.08 var(--display);letter-spacing:-.025em;margin:0;text-wrap:balance;overflow-wrap:break-word;min-width:0}
.lede{margin:0;color:var(--ink-2)}
.note{margin:0;font-size:14px;color:var(--muted)}
.lede b{font-weight:600;color:var(--ink)}
.grant{margin:0;padding:0;list-style:none;border-top:1px solid var(--line)}
.grant li{padding:var(--space-2) 0;border-bottom:1px solid var(--line);font-size:15px;color:var(--ink-2)}
form{display:grid;grid-template-columns:minmax(0,1fr);gap:var(--space-2)}
label{font-size:14px;font-weight:600;color:var(--ink);margin-top:var(--space-2)}
input{width:100%;min-width:0;font:inherit;font-size:16px;color:var(--ink);background:var(--surface);border:1px solid var(--line-2);
border-radius:var(--r-field);padding:var(--space-3) var(--space-4);transition:border-color .15s var(--ease)}
input::placeholder{color:var(--muted)}
input:hover{border-color:var(--muted)}
input:focus-visible,input:focus{outline:2px solid var(--focus);outline-offset:1px;border-color:var(--ink)}
input:disabled{background:var(--paper);color:var(--muted)}
input.code{font:500 28px/1 var(--mono);text-align:center;padding:var(--space-4)}
.hint{margin:0;font-size:14px;color:var(--muted)}
.fixed{margin:0;display:flex;flex-wrap:wrap;justify-content:space-between;gap:var(--space-2);font-size:14px;color:var(--muted);
padding:var(--space-3) var(--space-4);border-radius:var(--r-field);background:var(--paper)}
.fixed b{font-weight:600;color:var(--ink)}
button{margin-top:var(--space-4);font:600 16px/1 var(--body);color:var(--on-ink);background:var(--ink);border:0;
border-radius:var(--r-field);padding:var(--space-4) var(--space-5);cursor:pointer;transition:background-color .15s var(--ease)}
button:hover{background:var(--accent-ink)}
button:active{background:var(--ink-2)}
button:focus-visible{outline:2px solid var(--focus);outline-offset:2px}
button:disabled{background:var(--line);color:var(--ink-2);cursor:not-allowed}
.again{margin:0;font-size:14px}
a{color:var(--accent-ink);text-underline-offset:3px}
a:hover{color:var(--ink)}
a:focus-visible{outline:2px solid var(--focus);outline-offset:2px;border-radius:2px}
.error{margin:0;border-radius:var(--r-field);padding:var(--space-3) var(--space-4);background:var(--error-bg);
color:var(--error-ink);font-size:15px}
.foot{margin:0;color:var(--muted);font-size:14px}
@media (max-width:420px){.card{padding:var(--space-6)}h1{font-size:28px}}
@media (prefers-reduced-motion:reduce){input,button{transition:none}}
"""


def _page(
    lang: str,
    *,
    step: int | None = None,
    form: str = "",
    redirect: str | None = None,
    error: str | None = None,
    status: int = 200,
) -> HTMLResponse:
    t = T[lang]
    progress = (
        f'<div class="bar" role="img" aria-label="{t["step"].format(n=step)}">'
        f'<span class="on"></span><span class="{"on" if step == 2 else ""}"></span></div>'
        if step
        else ""
    )
    h1 = t["h1_code"] if step == 2 else t["h1"]
    err = f'<p class="error" id="error" role="alert">{html.escape(error)}</p>' if error else ""
    body = f"""<!doctype html><html lang="{lang}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex"><meta name="description" content="{t["description"]}">
<title>{t["title"]}</title>
<link rel="stylesheet" href="/static/fonts/fonts.css"><style>{_STYLE}</style></head>
<body><main class="wrap">
<section class="card">{progress}
  <h1>{h1}</h1>{err}{form}</section>
</main></body></html>"""
    actions = "'self'" + (f" {redirect}" if redirect else "")  # Chrome applies form-action to the redirect too
    csp = (
        "default-src 'none'; style-src 'self' 'unsafe-inline'; font-src 'self'; img-src 'self' data:; "
        f"form-action {actions}; frame-ancestors 'none'; base-uri 'none'"
    )
    return HTMLResponse(
        body,
        status_code=status,
        headers={
            "Cache-Control": "no-store",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": csp,
            "X-Content-Type-Options": "nosniff",
            "Content-Language": lang,
            "Vary": "Accept-Language",
        },
    )
