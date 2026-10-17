"""Live chat with the shop's sales team (staff takeover).

Every customer chat is also kept here as a simple transcript, so the staff page can follow it.
- A customer taps "Chat with our sales team": staff who are online get an alert.
- A staff member taps Join (on any chat): the AI pauses for that chat and the staff member writes.
- "Hand back to AI": the AI continues, and is told what the staff member and the customer said.
- If nobody joins within WAIT_SECONDS, the customer gets the WhatsApp button / contact form instead.
Everything is kept in memory only (like the AI chats): a restart clears it.
"""

import threading
import time

WAIT_SECONDS = 60           # how long a customer waits for a staff member before the fallback
STAFF_ONLINE_SECONDS = 20   # a staff page that checked in this recently counts as online
STAFF_GONE_SECONDS = 90     # a joined staff member whose page stopped checking in: the AI takes over again
KEEP_SECONDS = 3 * 3600     # chats quiet for longer than this leave the staff list
MAX_ENTRIES = 400           # per chat; the oldest lines are dropped first

# What the customer's page is told about (staff messages and changes of who is answering).
CUSTOMER_EVENTS = ("joined", "left", "timeout")


class LiveChats:
    def __init__(self, clock=time.time):
        self.clock = clock
        self._lock = threading.Lock()
        self._chats: dict[str, dict] = {}       # "shop:session" -> chat
        self._staff_seen: dict[str, float] = {}  # shop_id -> last time a staff page checked in

    # ---------- shared ----------

    def _chat(self, shop_id: str, session_id: str, language: str = "") -> dict:
        key = f"{shop_id}:{session_id}"
        chat = self._chats.get(key)
        if chat is None:
            now = self.clock()
            chat = self._chats[key] = {
                "shop": shop_id, "session": session_id, "language": language or "ar", "started": now,
                "updated": now, "mode": "ai", "staff_name": "", "wait_started": 0.0, "request": "",
                "join_seq": 0, "entries": [], "seq": 0, "note_for_ai": "", "customer_seen": now}
        if language:
            chat["language"] = language
        return chat

    def _add(self, chat: dict, who: str, text: str = "", event: str = "", name: str = "") -> dict:
        chat["seq"] += 1
        entry = {"seq": chat["seq"], "who": who, "text": text, "at": self.clock()}
        if event:
            entry["event"] = event
        if name:
            entry["name"] = name
        chat["entries"].append(entry)
        del chat["entries"][:-MAX_ENTRIES]
        chat["updated"] = entry["at"]
        return entry

    def _back_to_ai(self, chat: dict, why: str) -> None:
        """The staff member's part of the chat is passed to the AI with the customer's next message."""
        lines = []
        for e in chat["entries"]:
            if e["seq"] > chat["join_seq"] and e["who"] in ("customer", "staff") and e["text"]:
                who = "Customer" if e["who"] == "customer" else f"{e.get('name', 'Staff')} (sales team)"
                lines.append(f"{who}: {e['text']}")
        name = chat["staff_name"] or "A staff member"
        talk = "\n".join(lines[-30:])[-3000:] or "(nothing was written)"
        chat["note_for_ai"] = (
            f"(Chat window note: {name} from the shop's sales team talked with the customer in this chat:\n"
            f"{talk}\nThe sales team has handed the chat back to you. Continue from here. Do not repeat what "
            "the team said, and keep any promise they made.)")
        self._add(chat, "system", event="left", name=chat["staff_name"], text=why)
        chat["mode"], chat["staff_name"] = "ai", ""

    def _expire(self, chat: dict) -> None:
        now = self.clock()
        if chat["mode"] == "waiting" and now - chat["wait_started"] > WAIT_SECONDS:
            chat["mode"] = "ai"
            self._add(chat, "system", event="timeout", text="Nobody joined in time.")
        elif chat["mode"] == "staff" and now - self._staff_seen.get(chat["shop"], 0) > STAFF_GONE_SECONDS:
            self._back_to_ai(chat, "The staff page was closed.")

    # ---------- the customer's side ----------

    def record(self, shop_id: str, session_id: str, who: str, text: str, language: str = "") -> None:
        """who: "customer", "agent" (the AI) or "note" (shown to staff only, e.g. a form was sent)."""
        with self._lock:
            chat = self._chat(shop_id, session_id, language)
            self._add(chat, who, text)
            if who == "customer":
                chat["customer_seen"] = self.clock()

    def mode(self, shop_id: str, session_id: str) -> str:
        """"ai", "waiting" (asked for a person; the AI still answers) or "staff" (the AI is paused)."""
        with self._lock:
            chat = self._chats.get(f"{shop_id}:{session_id}")
            if chat is None:
                return "ai"
            self._expire(chat)
            return chat["mode"]

    def take_note_for_ai(self, shop_id: str, session_id: str) -> str:
        with self._lock:
            chat = self._chats.get(f"{shop_id}:{session_id}")
            if chat is None:
                return ""
            note, chat["note_for_ai"] = chat["note_for_ai"], ""
            return note

    def request_person(self, shop_id: str, session_id: str, request: str, language: str) -> str:
        """The customer wants a person. Returns "waiting", "staff" (already with one) or "offline"."""
        with self._lock:
            chat = self._chat(shop_id, session_id, language)
            self._expire(chat)
            if chat["mode"] == "staff":
                return "staff"
            if not self._online(shop_id):
                self._add(chat, "note", text="Customer asked for a person; no staff were online.")
                return "offline"
            if chat["mode"] != "waiting":
                chat["mode"], chat["wait_started"] = "waiting", self.clock()
                chat["request"] = request[:300]
                self._add(chat, "system", event="waiting", text=request[:300])
            return "waiting"

    def customer_updates(self, shop_id: str, session_id: str, after: int) -> dict:
        """Staff messages and changes since `after`, for the customer's page (which checks every few seconds)."""
        with self._lock:
            chat = self._chats.get(f"{shop_id}:{session_id}")
            if chat is None:
                return {"mode": "ai", "staff_name": "", "seq": 0, "events": []}
            chat["customer_seen"] = self.clock()
            self._expire(chat)
            events = [{k: e[k] for k in ("seq", "who", "text", "event", "name") if k in e}
                      for e in chat["entries"] if e["seq"] > after and
                      (e["who"] == "staff" or (e["who"] == "system" and e.get("event") in CUSTOMER_EVENTS))]
            for e in events:
                if e["who"] == "system":
                    e.pop("text", None)  # internal wording, not for the customer
            return {"mode": chat["mode"], "staff_name": chat["staff_name"], "seq": chat["seq"], "events": events}

    # ---------- the staff side ----------

    def _online(self, shop_id: str) -> bool:
        return self.clock() - self._staff_seen.get(shop_id, 0) <= STAFF_ONLINE_SECONDS

    def staff_online(self, shop_id: str) -> bool:
        with self._lock:
            return self._online(shop_id)

    def staff_check_in(self, shop_id: str) -> None:
        with self._lock:
            self._staff_seen[shop_id] = self.clock()

    def list_chats(self, shop_id: str) -> list:
        """The shop's recent chats: customers waiting for a person first, then the newest."""
        with self._lock:
            now = self.clock()
            self._staff_seen[shop_id] = now
            for key in [k for k, c in self._chats.items() if now - c["updated"] > KEEP_SECONDS]:
                del self._chats[key]
            out = []
            for chat in self._chats.values():
                if chat["shop"] != shop_id:
                    continue
                self._expire(chat)
                said = [e for e in chat["entries"] if e["who"] in ("customer", "agent", "staff") and e["text"]]
                out.append({
                    "session_id": chat["session"], "language": chat["language"], "mode": chat["mode"],
                    "staff_name": chat["staff_name"], "request": chat["request"],
                    "wait_left": max(0, round(WAIT_SECONDS - (now - chat["wait_started"])))
                    if chat["mode"] == "waiting" else 0,
                    "last": said[-1]["text"][:140] if said else "", "last_who": said[-1]["who"] if said else "",
                    "messages": sum(1 for e in said if e["who"] == "customer"),
                    "updated": chat["updated"], "started": chat["started"], "seq": chat["seq"],
                    "customer_active": now - chat["customer_seen"] < 30})
            out.sort(key=lambda c: (c["mode"] != "waiting", -c["updated"]))
            return out

    def chat_entries(self, shop_id: str, session_id: str, after: int = 0) -> dict | None:
        with self._lock:
            chat = self._chats.get(f"{shop_id}:{session_id}")
            if chat is None:
                return None
            self._expire(chat)
            return {"mode": chat["mode"], "staff_name": chat["staff_name"], "language": chat["language"],
                    "seq": chat["seq"], "entries": [dict(e) for e in chat["entries"] if e["seq"] > after]}

    def join(self, shop_id: str, session_id: str, staff_name: str) -> bool:
        with self._lock:
            chat = self._chats.get(f"{shop_id}:{session_id}")
            if chat is None:
                return False
            self._staff_seen[shop_id] = self.clock()
            if chat["mode"] == "staff" and chat["staff_name"] == staff_name:
                return True
            if chat["mode"] != "staff":
                chat["join_seq"] = chat["seq"]
            chat["mode"], chat["staff_name"] = "staff", staff_name
            self._add(chat, "system", event="joined", name=staff_name)
            return True

    def staff_send(self, shop_id: str, session_id: str, staff_name: str, text: str) -> bool:
        with self._lock:
            chat = self._chats.get(f"{shop_id}:{session_id}")
            if chat is None or chat["mode"] != "staff":
                return False
            self._staff_seen[shop_id] = self.clock()
            self._add(chat, "staff", text, name=staff_name)
            return True

    def hand_back(self, shop_id: str, session_id: str) -> bool:
        with self._lock:
            chat = self._chats.get(f"{shop_id}:{session_id}")
            if chat is None or chat["mode"] != "staff":
                return False
            self._back_to_ai(chat, "Handed back to the AI.")
            return True
