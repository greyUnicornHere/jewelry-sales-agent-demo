"""Small web server for the chat page.

Run:  python server.py   (or double-click start.bat)
Open: http://localhost:8000/chat?shop=demo-shop
"""

import hmac
import html
import json
import os
import socket
import sys
import threading
import time
import traceback
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

ROOT = Path(__file__).parent


def load_env_file() -> None:
    """Read KEY=VALUE lines from .env into the environment."""
    env_file = ROOT / ".env"
    if not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            if value.strip():
                os.environ.setdefault(key.strip(), value.strip().strip('"'))


class _Tee:
    """Show output in the window and also save it to app_log.txt (for checking what happened later)."""

    def __init__(self, stream, path):
        self.stream, self.file = stream, open(path, "a", encoding="utf-8", buffering=1)

    def write(self, text):
        self.stream.write(text)
        self.stream.flush()
        self.file.write(text)

    def flush(self):
        self.stream.flush()
        self.file.flush()


load_env_file()
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import anthropic  # noqa: E402  (after .env is loaded)

import gold_rates  # noqa: E402
import live_chat  # noqa: E402
import shop_tools  # noqa: E402
from agent import Agent  # noqa: E402

PORT = int(os.environ.get("PORT", "8000"))
# The shop that opens by default (e.g. for a client demo): "--shop <shop folder>" or DEFAULT_SHOP in .env.
DEFAULT_SHOP = os.environ.get("DEFAULT_SHOP", "demo-shop")
if "--shop" in sys.argv[:-1]:
    DEFAULT_SHOP = sys.argv[sys.argv.index("--shop") + 1]

# Settings for running online (all optional; set them on the hosting service, see render.yaml):
# ALLOWED_SHOPS   only these shops can be opened, e.g. "demo-shop" (other shop folders stay hidden)
# DEMO_PASSWORD   visitors must enter it once (or open a link with ?key=<password>)
# DAILY_MESSAGE_LIMIT / CHAT_MESSAGE_LIMIT  customer messages the AI answers per day / per chat (0 = no limit)
ALLOWED_SHOPS = {s.strip() for s in os.environ.get("ALLOWED_SHOPS", "").split(",") if s.strip()}
DEMO_PASSWORD = os.environ.get("DEMO_PASSWORD", "").strip()
DAILY_MESSAGE_LIMIT = int(os.environ.get("DAILY_MESSAGE_LIMIT", "0") or 0)
CHAT_MESSAGE_LIMIT = int(os.environ.get("CHAT_MESSAGE_LIMIT", "0") or 0)
_usage = {"day": "", "count": 0, "chats": {}}
_usage_guard = threading.Lock()


def shop_allowed(shop_id: str) -> bool:
    return shop_tools.shop_exists(shop_id) and (not ALLOWED_SHOPS or shop_id in ALLOWED_SHOPS)


def take_message_allowance(shop_id: str, session_id: str) -> str:
    """"" when the AI may answer; otherwise "day" or "chat" (that limit is reached). Counts the message."""
    with _usage_guard:
        today = datetime.now(gold_rates.RIYADH).date().isoformat()
        if _usage["day"] != today:
            _usage.update(day=today, count=0, chats={})
        key = f"{shop_id}:{session_id}"
        if DAILY_MESSAGE_LIMIT and _usage["count"] >= DAILY_MESSAGE_LIMIT:
            return "day"
        if CHAT_MESSAGE_LIMIT and _usage["chats"].get(key, 0) >= CHAT_MESSAGE_LIMIT:
            return "chat"
        _usage["count"] += 1
        _usage["chats"][key] = _usage["chats"].get(key, 0) + 1
        return ""


LIMIT_MESSAGES = {
    "day": "عذرًا، وصلنا للحد اليومي لمحادثات العرض التجريبي. جرّب مرة ثانية بكرة.\n"
           "Sorry, this demo has reached today's chat limit. Please try again tomorrow.",
    "chat": "وصلت هذي المحادثة للحد الأقصى في العرض التجريبي. افتح محادثة جديدة للمتابعة.\n"
            "This demo chat has reached its message limit. Please start a new chat to continue.",
}


def clean_label(value) -> str:
    """A shop name typed into a demo link: one short line of plain text."""
    text = " ".join(str(value or "").split())
    return "".join(ch for ch in text if ch.isalnum() or ch in " '&-.")[:60].strip()


def _demo_token() -> str:
    return hmac.new(DEMO_PASSWORD.encode(), b"jewelry-demo-access", "sha256").hexdigest()


def safe_next(path: str) -> str:
    """Only paths on this site (never another site) after signing in."""
    return path if path.startswith("/") and not path.startswith("//") and "\\" not in path else "/"
IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
               ".webp": "image/webp", ".svg": "image/svg+xml"}
CHAT_PAGE_FILE = ROOT / "web" / "chat.html"
STAFF_PAGE_FILE = ROOT / "web" / "staff.html"
agent = Agent() if os.environ.get("ANTHROPIC_API_KEY") else None
live = live_chat.LiveChats()  # chats as the sales team sees them (live takeover)
_last_warm: dict[str, float] = {}
_pin_failures: dict[str, list] = {}  # computer address -> [wrong PINs, time of the first one]
_pin_guard = threading.Lock()
PIN_TRIES, PIN_LOCK_SECONDS = 8, 300


def _check_secret(expected: str, given: str, address: str) -> str:
    """"ok", "wrong", "locked" (too many wrong tries from this address) or "off" (no secret is set)."""
    with _pin_guard:
        failures = _pin_failures.get(address)
        if failures and time.time() - failures[1] > PIN_LOCK_SECONDS:
            del _pin_failures[address]
            failures = None
        if failures and failures[0] >= PIN_TRIES:
            return "locked"
        if not expected:
            return "off"
        if hmac.compare_digest(given.encode(), expected.encode()):
            _pin_failures.pop(address, None)
            return "ok"
        entry = _pin_failures.setdefault(address, [0, time.time()])
        entry[0] += 1
        return "wrong"


def staff_pin_ok(shop_id: str, pin: str, address: str) -> str:
    return _check_secret(str(shop_tools.load_settings(shop_id).get("staff_pin", "")).strip(), pin, address)


def demo_password_ok(password: str, address: str) -> bool:
    return _check_secret(DEMO_PASSWORD, password.strip(), address) == "ok"


def describe_reply(events: list) -> str:
    """The AI's reply as one readable text for the staff page (cards and buttons in brackets)."""
    parts, text = [], []  # parts: (streamed text?, line)
    for e in events:
        kind = e["type"]
        if kind == "text":
            text.append(e["text"])
        elif kind in ("retry", "retract"):  # like the chat page: that text is no longer shown
            text = []
            if kind == "retract":
                parts = [p for p in parts if not p[0]]
        else:
            if "".join(text).strip():
                parts.append((True, "".join(text).strip()))
            text = []
            if kind == "message":
                parts.append((False, e["text"]))
            elif kind == "products":
                parts.append((False, "[Cards: " + ", ".join(f"{i['id']} {i['name']}" for i in e["items"]) + "]"))
            elif kind == "choices":
                parts.append((False, "[Buttons: " + " / ".join(e["options"]) + "]"))
            elif kind == "contact_sales":
                parts.append((False, "[Contact sales button]"))
            elif kind == "form":
                parts.append((False, "[Booking form]" if e["kind"] == "booking" else "[Contact form]"))
            elif kind == "error":
                parts.append((False, "[Error: " + e["error"] + "]"))
    if "".join(text).strip():
        parts.append((True, "".join(text).strip()))
    return "\n".join(line for _, line in parts)


def warm_shop(shop_id: str) -> None:
    """Get the AI ready for this shop in the background (at start and when the chat page opens).
    The 1-hour cache means once every 50 minutes is enough."""
    if agent is None or time.time() - _last_warm.get(shop_id, 0) < 50 * 60:
        return
    _last_warm[shop_id] = time.time()

    def run():
        try:
            agent.warm_up(shop_id)
            print(f"  [warm-up] {shop_id}: ready")
        except Exception as e:  # warming is only a speed-up; never break the chat for it
            _last_warm[shop_id] = 0
            print(f"  [warm-up] {shop_id}: skipped ({type(e).__name__})")

    threading.Thread(target=run, daemon=True).start()


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, data: dict) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def client_ip(self) -> str:
        """The visitor's address (online, the hosting service passes it on in X-Forwarded-For)."""
        forwarded = self.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        return forwarded or self.client_address[0]

    def _signed_in(self) -> bool:
        if not DEMO_PASSWORD:
            return True
        cookies = dict(c.strip().split("=", 1) for c in self.headers.get("Cookie", "").split(";") if "=" in c)
        return hmac.compare_digest(cookies.get("demo_access", ""), _demo_token())

    def _sign_in_cookie(self) -> str:
        secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
        return f"demo_access={_demo_token()}; Path=/; Max-Age={30 * 86400}; HttpOnly; SameSite=Lax{secure}"

    def _gate(self, url) -> bool:
        """With DEMO_PASSWORD set, visitors sign in once. Returns True when this request was answered here."""
        if not DEMO_PASSWORD or self._signed_in():
            return False
        query = parse_qs(url.query)
        key = query.pop("key", [""])[0]
        if key and demo_password_ok(key, self.client_ip()):  # a link with ?key=: sign in and drop the key
            rest = "&".join(f"{k}={quote(v[0])}" for k, v in query.items())
            self.send_response(302)
            self.send_header("Set-Cookie", self._sign_in_cookie())
            self.send_header("Location", url.path + ("?" + rest if rest else ""))
            self.end_headers()
        elif url.path.startswith("/api/") or url.path.startswith("/shop-images/"):
            self._json(401, {"error": "Please open the demo link again and enter the password."})
        else:
            page = LOGIN_PAGE.replace("__NEXT__", html.escape(safe_next(self.path), quote=True))
            page = page.replace("__WRONG__", "hidden" if not key else "")
            self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
        return True

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/health":  # the hosting service checks that the app is up
            return self._send(200, b"ok", "text/plain")
        if self._gate(url):
            return
        if url.path == "/":
            self.send_response(302)
            self.send_header("Location", f"/chat?shop={DEFAULT_SHOP}")
            self.end_headers()
        elif url.path == "/chat":
            # Read on every visit, so changes to the page show after a browser refresh.
            self._send(200, CHAT_PAGE_FILE.read_bytes(), "text/html; charset=utf-8")
        elif url.path == "/api/shop":
            shop_id = parse_qs(url.query).get("shop", [""])[0]
            if not shop_allowed(shop_id):
                return self._json(404, {"error": "Shop not found"})
            warm_shop(shop_id)  # the customer is choosing a language: a good moment to get the AI ready
            s = shop_tools.load_settings(shop_id)
            fields =("shop_name_ar", "shop_name_en", "greeting_ar", "greeting_en", "human_reply_time", "human_reply_time_ar")
            info = {k: s.get(k, "") for k in fields}
            info["sales_whatsapp"] = "".join(ch for ch in str(s.get("sales_whatsapp", "")) if ch.isdigit())
            info["visit_slots"] = s.get("visit_slots", [])
            info["visit_slots_friday"] = s.get("visit_slots_friday", [])
            today = datetime.now(gold_rates.RIYADH).date()
            info["today"] = today.isoformat()  # booking form starts at today (Riyadh)
            info["today_slots"] = shop_tools.open_visit_slots(s, today)  # today: only times not yet passed
            self._json(200, info)
        elif url.path == "/api/live":  # the customer's page checks for staff messages every few seconds
            query = parse_qs(url.query)
            shop_id = query.get("shop", [""])[0]
            session_id = query.get("session_id", [""])[0][:64]
            if not shop_allowed(shop_id) or not session_id:
                return self._json(404, {"error": "Not found"})
            try:
                after = int(query.get("after", ["0"])[0])
            except ValueError:
                after = 0
            self._json(200, live.customer_updates(shop_id, session_id, after))
        elif url.path == "/staff":
            self._send(200, STAFF_PAGE_FILE.read_bytes(), "text/html; charset=utf-8")
        elif url.path == "/make-link":  # for the person giving demos: a chat link with the prospect's shop name
            shop_id = parse_qs(url.query).get("shop", [DEFAULT_SHOP])[0]
            if not shop_allowed(shop_id):
                return self._json(404, {"error": "Shop not found"})
            js = lambda value: json.dumps(value).replace("<", "\\u003c")  # safe inside <script>
            page = LINK_PAGE.replace("__SHOP__", js(shop_id)).replace("__KEY__", js(DEMO_PASSWORD))
            self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
        elif url.path in ("/api/staff/chats", "/api/staff/chat"):
            query = parse_qs(url.query)
            shop_id = query.get("shop", [""])[0]
            if not self._staff_allowed(shop_id):
                return
            if url.path == "/api/staff/chats":
                s = shop_tools.load_settings(shop_id)
                return self._json(200, {"shop_name_en": s.get("shop_name_en", ""), "shop_name_ar": s.get("shop_name_ar", ""),
                                        "wait_seconds": live_chat.WAIT_SECONDS, "now": time.time(),
                                        "chats": live.list_chats(shop_id)})
            try:
                after = int(query.get("after", ["0"])[0])
            except ValueError:
                after = 0
            live.staff_check_in(shop_id)
            data = live.chat_entries(shop_id, query.get("session_id", [""])[0][:64], after)
            self._json(200, data) if data else self._json(404, {"error": "This chat has ended."})
        elif url.path == "/phone" and "__URL__" not in PHONE_PAGE:  # only in phone demo mode
            self._send(200, PHONE_PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif url.path.startswith("/shop-images/"):
            parts = url.path.split("/")
            path = shop_tools.image_path(parts[2], parts[3]) if len(parts) == 4 and shop_allowed(parts[2]) else None
            if path is None:
                return self._json(404, {"error": "Image not found"})
            self._send(200, path.read_bytes(), IMAGE_TYPES[path.suffix.lower()])
        else:
            self._json(404, {"error": "Not found"})

    def _read_raw(self) -> bytes:
        try:
            return self.rfile.read(min(int(self.headers.get("Content-Length", "0")), 20000))
        except ValueError:
            return b""

    @staticmethod
    def _parse_json(raw: bytes) -> dict | None:
        try:
            body = json.loads(raw or b"{}")
            return body if isinstance(body, dict) else None
        except (ValueError, json.JSONDecodeError):
            return None

    def _handle_login(self, raw: bytes):
        """The demo password page sends the password here (a plain form)."""
        form = parse_qs(raw.decode("utf-8", "replace"))
        target = safe_next(form.get("next", ["/"])[0])
        if demo_password_ok(form.get("password", [""])[0], self.client_ip()):
            self.send_response(303)
            self.send_header("Set-Cookie", self._sign_in_cookie())
            self.send_header("Location", target)
            self.end_headers()
        else:
            page = LOGIN_PAGE.replace("__NEXT__", html.escape(target, quote=True)).replace("__WRONG__", "")
            self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")

    def _staff_allowed(self, shop_id: str) -> bool:
        """Staff pages and actions need the shop's staff PIN. Sends the error answer when not allowed."""
        if not shop_allowed(shop_id):
            self._json(404, {"error": "Shop not found"})
            return False
        result = staff_pin_ok(shop_id, self.headers.get("X-Staff-Pin", ""), self.client_ip())
        if result == "ok":
            return True
        messages = {"wrong": "Wrong PIN.", "locked": "Too many wrong PINs. Wait 5 minutes and try again.",
                    "off": "This shop has no staff PIN yet. Add \"staff_pin\" to its settings.json."}
        self._json(403, {"error": messages[result], "reason": result})
        return False

    def _handle_staff_action(self, path: str, shop_id: str, session_id: str, body: dict):
        """Join a chat (the AI pauses), write to the customer, or hand the chat back to the AI."""
        if not self._staff_allowed(shop_id):
            return
        name = " ".join(str(body.get("name", "")).split())[:40]
        if path == "/api/staff/join":
            if not name:
                return self._json(400, {"error": "Please write your name first."})
            ok = live.join(shop_id, session_id, name)
            if ok:
                print(f"[{shop_id}/{session_id[:8]}] staff: {name} joined")
        elif path == "/api/staff/send":
            text = str(body.get("text", "")).strip()[:2000]
            if not text or not name:
                return self._json(400, {"error": "Empty message"})
            ok = live.staff_send(shop_id, session_id, name, text)
            if ok:
                print(f"[{shop_id}/{session_id[:8]}] staff {name}: {text}")
        else:
            ok = live.hand_back(shop_id, session_id)
            if ok:
                print(f"[{shop_id}/{session_id[:8]}] staff: handed back to the AI")
        if not ok:
            return self._json(409, {"error": "This chat has changed. Please look again."})
        self._json(200, {"ok": True})

    def do_POST(self):
        path = urlparse(self.path).path
        raw = self._read_raw()  # read first, so the connection is never cut while the browser is still sending
        if path == "/login":
            return self._handle_login(raw)
        if not self._signed_in():
            return self._json(401, {"error": "Please open the demo link again and enter the password."})
        body = self._parse_json(raw)
        staff_paths = ("/api/staff/join", "/api/staff/send", "/api/staff/leave")
        if path not in ("/api/chat", "/api/request", "/api/reserve", "/api/contact", "/api/handoff") + staff_paths:
            return self._json(404, {"error": "Not found"})
        if body is None:
            return self._json(400, {"error": "Bad request"})
        shop_id = str(body.get("shop", ""))
        session_id = str(body.get("session_id", ""))[:64]
        if path in staff_paths:
            return self._handle_staff_action(path, shop_id, session_id, body)
        if not shop_allowed(shop_id):
            return self._json(404, {"error": "Shop not found"})
        if path == "/api/handoff":  # "Chat with our sales team": alert the staff who are online
            if not session_id:
                return self._json(400, {"error": "Bad request"})
            language = "en" if body.get("language") == "en" else "ar"
            status = live.request_person(shop_id, session_id, str(body.get("request", "")), language)
            print(f"[{shop_id}/{session_id[:8]}] customer asked for a person: {status}")
            return self._json(200, {"status": status, "wait_seconds": live_chat.WAIT_SECONDS})
        if agent is None:
            return self._json(503, {"error": "The API key is missing. Put your key in the .env file and restart."})
        if path == "/api/request":
            return self._handle_request_form(shop_id, session_id, body)
        # A product the customer tapped "Choose" on (must be in this shop's catalog).
        item_id = str(body.get("item_id", ""))
        item = next((i for i in shop_tools.load_catalog(shop_id) if i["id"] == item_id), None) if item_id else None
        if path == "/api/contact":  # "Contact sales" when the shop has no WhatsApp number: the contact form
            if not session_id:
                return self._json(400, {"error": "Bad request"})
            language = "en" if body.get("language") == "en" else "ar"
            return self._json(200, agent.contact_form(shop_id, session_id, str(body.get("request", "")), language))
        if path == "/api/reserve":
            if item is None or not session_id:
                return self._json(400, {"error": "Unknown piece"})
            language = "en" if body.get("language") == "en" else "ar"
            return self._json(200, agent.reserve_form(shop_id, session_id, item, language))

        message = str(body.get("message", "")).strip()[:2000]
        language = "en" if body.get("language") == "en" else "ar"
        if not session_id or not message:
            return self._json(400, {"error": "Empty message"})

        print(f"[{shop_id}/{session_id[:8]}] customer: {message}")
        live.record(shop_id, session_id, "customer", message, language)
        # Stream the answer: one JSON event per line, sent as soon as it is ready.
        # The response ends when the connection closes (HTTP/1.0).
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        def emit(event: dict) -> None:
            self.wfile.write((json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8"))
            self.wfile.flush()

        if live.mode(shop_id, session_id) == "staff":  # a staff member is answering: the AI stays quiet
            try:
                emit({"type": "with_staff"})
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        limit = take_message_allowance(shop_id, session_id)
        if limit:  # online demo: keeps the AI bill under control
            print(f"  [limit] {limit} limit reached")
            live.record(shop_id, session_id, "note", f"Message limit reached ({limit}); the AI did not answer.")
            try:
                emit({"type": "text", "text": LIMIT_MESSAGES[limit]})
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        label = body.get("shop_label") if isinstance(body.get("shop_label"), dict) else {}
        events = agent.reply(shop_id, session_id, message, language, chosen_item=item,
                             staff_note=live.take_note_for_ai(shop_id, session_id),
                             shop_label=(clean_label(label.get("ar")), clean_label(label.get("en"))))
        shown = []
        try:
            for event in events:
                if event["type"] == "contact_sales":
                    event["live"] = live.staff_online(shop_id)  # a person can join the chat right now
                shown.append(event)
                emit(event)
        except (BrokenPipeError, ConnectionResetError):
            print("  (customer closed the page)")
        except anthropic.AuthenticationError:
            emit({"type": "error", "error": "The API key in .env is not valid."})
        except anthropic.RateLimitError:
            emit({"type": "error", "error": "Too many requests right now. Please wait a minute and try again."})
        except anthropic.APIConnectionError:
            emit({"type": "error", "error": "Cannot reach Claude. Check the internet connection."})
        except anthropic.APIStatusError as e:
            print(f"API error {e.status_code}: {e.message}")
            emit({"type": "error", "error": f"Claude API error ({e.status_code}). Details are in the server window."})
        except Exception:
            traceback.print_exc()
            emit({"type": "error", "error": "Something went wrong. Details are in the server window."})
        finally:
            events.close()
            reply = describe_reply(shown)  # what the customer saw (text taken back is left out)
            live.record(shop_id, session_id, "agent", reply)
        print(f"[{shop_id}/{session_id[:8]}] agent: {reply}")

    def _handle_request_form(self, shop_id: str, session_id: str, body: dict):
        """The customer sent the contact form or the visit booking form. Save it for the shop team."""
        form = agent.get_form(shop_id, session_id, str(body.get("form_id", "")))
        if form is None:
            return self._json(400, {"error": "This form was already sent or has expired. Please ask again in the chat."})
        name = str(body.get("name", "")).strip()[:80]
        phone = str(body.get("phone", "")).strip()[:20]
        request_text = str(body.get("request", "")).strip()[:500]
        language = "en" if body.get("language") == "en" else "ar"
        if not name or not shop_tools.PHONE_PATTERN.match(phone) or body.get("consent") is not True:
            return self._json(400, {"error": "missing_fields"})

        if form["kind"] == "booking":
            day_text, time_text = str(body.get("date", "")), str(body.get("time", ""))
            if shop_tools.check_visit_time(shop_tools.load_settings(shop_id), day_text, time_text) is None:
                return self._json(400, {"error": "bad_time"})
            ref = shop_tools.save_booking(shop_id, form, name, phone, day_text, time_text, request_text, language)
            agent.form_sent(shop_id, session_id, str(body["form_id"]), name, ref, visit=f"{day_text} {time_text}")
        else:
            if not request_text:
                return self._json(400, {"error": "missing_fields"})
            ref = shop_tools.save_request(shop_id, form, name, phone, request_text, language)
            agent.form_sent(shop_id, session_id, str(body["form_id"]), name, ref)
        live.record(shop_id, session_id, "note", f"{name} sent the {'booking' if form['kind'] == 'booking' else 'contact'} "
                                                 f"form ({ref}).")
        self._json(200, {"reference": ref})

    def log_message(self, *args):  # keep the window readable
        pass


def lan_address() -> str:
    """This computer's address on the Wi-Fi (no data is sent; it only asks which network card would be used)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("192.0.2.1", 80))
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"


PHONE_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Open on your phone</title>
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#f7f4ee;color:#1d1a17;
font-family:-apple-system,"Segoe UI",system-ui,sans-serif;text-align:center}
h1{font-weight:600;font-size:26px;margin:0 0 6px}p{color:#5f574e;margin:6px 0}
#qr{background:#fff;padding:18px;border-radius:20px;box-shadow:0 18px 50px rgba(0,0,0,.12);display:inline-block;margin:22px 0}
code{font-size:18px;background:#fff;padding:8px 12px;border-radius:10px}</style></head>
<body><div><h1>Scan with your phone's camera</h1><p>Your phone must be on the same Wi-Fi as this computer.</p>
<div id="qr"></div><p>Or type this address in your phone's browser:</p><p><code>__URL__</code></p></div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js"></script>
<script>new QRCode(document.getElementById("qr"), {text: "__URL__", width: 260, height: 260});</script>
</body></html>"""


_PAGE_STYLE = """<style>
:root{--bg:#f7f4ee;--surface:#fff;--surface-2:#f1ece3;--ink:#1d1a17;--ink-2:#5f574e;--ink-3:#9a9086;
--line:rgba(29,26,23,.09);--gold:#a47a2c;--gold-2:#c9a45c;--gold-soft:rgba(164,122,44,.10);--primary:#1d1a17;--on-primary:#fff;
--danger:#a3341f}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#151311;--surface:#1d1a17;--surface-2:#262220;
--ink:#f3eee6;--ink-2:#b9afa3;--ink-3:#82786d;--line:rgba(243,238,230,.09);--gold:#d6b06a;--gold-2:#e6c88b;
--gold-soft:rgba(214,176,106,.12);--primary:#f3eee6;--on-primary:#151311;--danger:#f0a090}}
:root[data-theme="dark"]{--bg:#151311;--surface:#1d1a17;--surface-2:#262220;--ink:#f3eee6;--ink-2:#b9afa3;--ink-3:#82786d;
--line:rgba(243,238,230,.09);--gold:#d6b06a;--gold-2:#e6c88b;--gold-soft:rgba(214,176,106,.12);--primary:#f3eee6;
--on-primary:#151311;--danger:#f0a090}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;padding:24px 16px;background:var(--bg);
color:var(--ink);font-family:"IBM Plex Sans Arabic",-apple-system,"Segoe UI",system-ui,sans-serif;line-height:1.55}
.box{width:100%;max-width:420px;padding:28px;background:var(--surface);border:1px solid var(--line);border-radius:24px;
box-shadow:0 18px 50px rgba(0,0,0,.10);display:flex;flex-direction:column;gap:14px}
.logo{width:52px;height:52px;margin:0 auto 4px;border-radius:50%;display:grid;place-items:center;
background:linear-gradient(145deg,var(--gold-2),var(--gold))}
h1{margin:0;text-align:center;font-family:"Cormorant Garamond",Georgia,serif;font-weight:600;font-size:28px;line-height:1.2}
p{margin:0;text-align:center;color:var(--ink-2);font-size:14px}
label{display:flex;flex-direction:column;gap:6px;font-size:13px;font-weight:500;color:var(--ink-2)}
input{font:inherit;padding:12px 14px;font-size:16px;color:var(--ink);background:var(--surface-2);border:1px solid transparent;border-radius:12px}
input:focus{outline:none;background:var(--surface);border-color:var(--gold);box-shadow:0 0 0 4px var(--gold-soft)}
button{font:inherit;height:46px;border:0;border-radius:14px;font-weight:600;font-size:15px;background:var(--primary);
color:var(--on-primary);cursor:pointer}.problem{color:var(--danger);font-size:13.5px;text-align:center}
[hidden]{display:none!important}
</style>"""

_LOGO = ('<div class="logo"><svg width="24" height="24" viewBox="0 0 24 24" fill="none">'
         '<path d="M6 4h12l4 5-10 11L2 9l4-5Z" fill="#fff"/></svg></div>')

LOGIN_PAGE = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Jewelry Sales Agent Demo</title>{_PAGE_STYLE}</head>
<body><form class="box" method="post" action="/login">{_LOGO}
<h1>عرض تجريبي · Demo</h1><p>أدخل كلمة مرور العرض<br>Enter the demo password</p>
<input type="hidden" name="next" value="__NEXT__">
<label>كلمة المرور · Password<input name="password" type="password" autocomplete="current-password" required autofocus></label>
<div class="problem" __WRONG__>كلمة المرور غير صحيحة · Wrong password</div>
<button type="submit">دخول · Open</button></form></body></html>"""

LINK_PAGE = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Demo Link Maker</title>{_PAGE_STYLE}
<style>#qr{{align-self:center;background:#fff;padding:14px;border-radius:16px}}#qr:empty{{display:none}}
textarea{{font:inherit;font-size:13px;padding:10px 12px;border-radius:12px;border:1px solid var(--line);background:var(--surface-2);
color:var(--ink);resize:none;direction:ltr}}.row{{display:flex;gap:8px}}.row button{{flex:1}}
.check{{flex-direction:row;align-items:center;gap:8px;font-weight:400}}.check input{{width:18px;height:18px;accent-color:var(--gold)}}</style></head>
<body><div class="box">{_LOGO}<h1>Demo link maker</h1>
<p>Type the prospect's shop name. The chat opens with their name, and they can scan the code with their phone.</p>
<label>Shop name in Arabic<input id="ar" dir="rtl" placeholder="مثال: مجوهرات الفارس" maxlength="60"></label>
<label>Shop name in English<input id="en" dir="ltr" placeholder="Example: Al Faris Jewelry" maxlength="60"></label>
<label class="check"><input type="checkbox" id="withKey" checked>Open without the password (the link carries it)</label>
<button type="button" id="make">Make link and QR code</button>
<div id="qr"></div><textarea id="out" rows="3" readonly hidden></textarea>
<div class="row" id="actions" hidden><button type="button" id="copy">Copy link</button><button type="button" id="open">Open</button></div>
</div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js"></script>
<script>
const SHOP = __SHOP__, KEY = __KEY__;
const $ = (id) => document.getElementById(id);
$("withKey").closest("label").hidden = !KEY;
$("make").addEventListener("click", () => {{
  const p = new URLSearchParams({{shop: SHOP}});
  if ($("ar").value.trim()) p.set("name_ar", $("ar").value.trim());
  if ($("en").value.trim()) p.set("name_en", $("en").value.trim());
  if (KEY && $("withKey").checked) p.set("key", KEY);
  const url = location.origin + "/chat?" + p.toString();
  $("out").value = url; $("out").hidden = false; $("actions").hidden = false;
  $("qr").replaceChildren();
  try {{ new QRCode($("qr"), {{text: url, width: 220, height: 220}}); }} catch (e) {{}}
}});
$("copy").addEventListener("click", () => {{ $("out").select(); try {{ document.execCommand("copy"); $("copy").textContent = "Copied"; }} catch (e) {{}} }});
$("open").addEventListener("click", () => window.open($("out").value, "_blank"));
</script></body></html>"""


if __name__ == "__main__":
    sys.stdout = _Tee(sys.stdout, ROOT / "app_log.txt")
    sys.stderr = _Tee(sys.stderr, ROOT / "app_log.txt")
    phone_demo = "--phone" in sys.argv  # let phones on the same Wi-Fi open the chat
    url = f"http://localhost:{PORT}/chat?shop={DEFAULT_SHOP}"
    print(f"Jewelry sales agent running at {url}")
    if phone_demo:
        phone_url = f"http://{lan_address()}:{PORT}/chat?shop={DEFAULT_SHOP}"
        PHONE_PAGE = PHONE_PAGE.replace("__URL__", phone_url)
        print(f"\nPHONE DEMO: on your phone (same Wi-Fi), open  {phone_url}")
        print("If Windows asks whether to allow Python on networks, click Allow (private networks).\n")
        url = f"http://localhost:{PORT}/phone"
    if agent is None:
        print("WARNING: no ANTHROPIC_API_KEY found. Open the .env file, paste your key, then restart.")
    if os.environ.get("GOLDAPI_KEY") and not os.environ.get("METALS_DEV_API_KEY"):
        print("NOTE: no METALS_DEV_API_KEY found, so there is no automatic switch when the goldapi.io checks "
              "are used up. Get a free key at https://metals.dev, paste it in .env, then restart.")
    if not gold_rates.is_configured():
        print("WARNING: no GOLDAPI_KEY found, so shops with \"gold_rate_source\": \"auto\" use the SAMPLE "
              "gold rates. Get a key at https://www.goldapi.io, paste it in .env, then restart.")
    online = "--public" in sys.argv  # running on a hosting service: open to the internet
    if online:
        print(f"ONLINE MODE. Shops: {', '.join(sorted(ALLOWED_SHOPS)) or 'all'}. "
              f"Password: {'on' if DEMO_PASSWORD else 'OFF'}. Limits: {DAILY_MESSAGE_LIMIT or 'none'} messages a day, "
              f"{CHAT_MESSAGE_LIMIT or 'none'} per chat.")
        if not DEMO_PASSWORD:
            print("WARNING: no DEMO_PASSWORD set, so anyone with the link can use the AI.")
    print("Close this window (or press Ctrl+C) to stop.\n")
    if shop_allowed(DEFAULT_SHOP):
        warm_shop(DEFAULT_SHOP)
    if "--no-browser" not in sys.argv and not online:
        threading.Timer(1.0, webbrowser.open, [url]).start()
    ThreadingHTTPServer(("0.0.0.0" if phone_demo or online else "127.0.0.1", PORT), Handler).serve_forever()
