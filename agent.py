"""The agent: Claude + the sales method + the shop's tools.

One Agent object serves every shop. Each chat (session) keeps its own
conversation history in memory.
"""

import json
import os
import re
import threading
import uuid
from pathlib import Path

import anthropic

import shop_tools

MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5-5")
EFFORT = os.environ.get("CLAUDE_EFFORT", "low")  # low | medium | high
MAX_TOOL_ROUNDS = 8
# Models where the API can re-run a declined message on a fallback model (Haiku cannot).
FALLBACK_OPTIONS = (
    {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
    if MODEL in ("claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5-1")
    else {}
)

SALES_METHOD = (Path(__file__).parent / "prompts" / "sales_method.md").read_text(encoding="utf-8")

CHAT_FORMAT_RULES = """
# Chat window rules
- The chat window shows plain text only. Do not use markdown headings, tables, or bold. Short lines and simple dashes are fine.
- Keep each reply short: 1-4 sentences.
- To present pieces, use show_products. Do not list name, karat, weight, and price in your text; the cards show them. Never give a price breakdown (hard rule 2).
- show_products, ask_occasion, show_contact_sales, show_request_form and show_booking_form are always the last step of your reply (you may call several of them together). Put what you want to say in their message field, not in normal text. You will not be able to add text after them.
- Messages that start with "(Chat window note: ...)" come from the chat window, not typed by the customer.
- Everything you write as text is shown to the customer. Never write notes to yourself, your plan, or what you are about to do (no "The customer wants...", "Let me search...", no tool names). Think silently; write only what the customer should read.
- The chat window already welcomed the customer before their first message (for example "Welcome! How can I help you today?" or "حياك الله، كيف أقدر أخدمك؟"). Do not greet or welcome them again (no "Welcome", "Hello", "حياك الله", "أهلاً", "مرحبا" at the start of your replies). Start directly with a warm answer. If they greet you, answer the greeting briefly once.
- Latency-sensitive; begin your visible answer immediately.
- The customer cannot see tool results. Tell them what matters in your own words.
"""


# The shop does not want made-up family titles before a customer's name ("يا أبا عبدالله",
# "Abu Abdullah"). The instructions say so, but models still do it in Arabic, so the text is
# also cleaned before the customer sees it. Place names like "أبو ظبي" / "Abu Dhabi" stay.
_KUNYA = re.compile(r"(?:(?<=\s)|^|(?<=[،,.!؟?]))(?:أبا|أبو|ابو|ابا)\s+(?!ظبي|ظبى)(?=\S)")
_KUNYA_EN = re.compile(r"\b(?:Abu|Umm)\s+(?!Dhabi)(?=[A-Z])")
_KUNYA_WORDS = {"أبا", "أبو", "ابو", "ابا", "Abu", "Umm"}


# Words that already tell the occasion or purpose. When the customer used one, the agent may not ask
# the occasion again (the instructions say so, but the model sometimes asks anyway).
_OCCASION_HINTS = re.compile(
    r"(?i)\b(for (myself|me|a friend|someone|my (wife|mother|mom|mum|daughter|sister|son|husband|father|dad|"
    r"fianc[eé]e?|bride|friend|girlfriend|grandmother|aunt|family|parents))|gift|present|engagement|shabka|wedding|"
    r"anniversary|birthday|graduation|new ?born|baby|eid|ramadan|just looking|browsing)\b|"
    r"(?:^|[\s،.؟!])(?:و|ب|ل|لل|ال)?(لنفسي|هدية|هديه|خطوبة|خطوبه|شبكة|شبكه|زواج|عرس|ذكرى|عيد|تخرج|مولود|"
    r"رمضان|أتفرج|اتفرج|لزوجتي|لزوجي|لأمي|لامي|لوالدتي|لبنتي|لأختي|لاختي|لصديقتي|لولدي|لأبوي|لابوي|لي)"
    r"(?=$|[\s،.؟!])")


# The occasion is asked only once the customer has mentioned a product (the shop's rule).
_PRODUCT_WORDS = re.compile(
    r"(?i)\b(rings?|necklaces?|chains?|pendants?|bracelets?|bangles?|cuffs?|anklets?|earrings?|studs|hoops|sets?|"
    r"jewel(?:le)?ry|pieces?|shabka|bars?)\b|"
    r"(خاتم|خواتم|دبلة|دبل|عقد|عقود|سلسال|سلاسل|سلسلة|قلادة|أسورة|اسوارة|اسواره|إسوارة|أساور|اساور|سوار|غويشة|"
    r"حلق|أقراط|اقراط|قرط|خلخال|طقم|أطقم|شبكة|شبكه|مجوهرات|قطعة|قطع|سبيكة|سبائك)")


# A question only about the price or details of a piece the customer named does not need the occasion.
_PRICE_OR_DETAIL = re.compile(
    r"(?i)\b(price|prices|cost|costs|how much|breakdown|making|labou?r|weight|weigh|weighs|karat|carat|details?|size|sizes|"
    r"available|in stock|stones?|made of|what gold|brand|brands|made by|is (?:it|this|that) from|original|genuine|"
    r"cartier|bvlgari|bulgari|tiffany|van cleef|chopard|dior|chanel|herm[eè]s|gucci|damas|l'?azurde)\b|"
    r"(سعر|اسعار|أسعار|بكم|كم يكلف|تفصيل|تفاصيل|مصنعية|مصنعيه|وزن|عيار|مقاس|مقاسات|متوفر|متوفرة|فصوص|كم سعر|ماركة|ماركه|براند|أصلي|اصلي|"
    r"كارتير|كارتييه|بولغاري|بلغاري|تيفاني|داماس|لازوردي)")
_GENERIC_NAME_WORDS = {
    "ring", "rings", "bracelet", "bangle", "necklace", "chain", "earrings", "earring", "studs", "gold", "white",
    "yellow", "with", "18k", "21k", "22k", "24k", "small", "thin", "wide", "open", "stone", "stones", "set",
    "خاتم", "أسورة", "اسوارة", "عقد", "سلسال", "حلق", "ذهب", "عيار", "أبيض", "أصفر", "صغير", "رفيع", "عريض",
}


def _name_words(text: str) -> set:
    """Words of a text, Arabic words also without "ال", so "الثلاثي" matches "ثلاثي"."""
    words = set(re.findall(r"\w+", text.lower()))
    return words | {w[2:] for w in words if w.startswith("ال") and len(w) > 4}


def named_piece(shop_id: str, text: str) -> str:
    """The id of a catalog piece whose distinctive name words appear in the text ('' if none)."""
    words = _name_words(text)
    for item in shop_tools.load_catalog(shop_id):
        name_words = _name_words(f"{item['name_en']} {item['name_ar']}")
        distinctive = {w for w in name_words - _GENERIC_NAME_WORDS if len(w) >= 4 and not w.isdigit()}
        if item["id"].lower() in words or words & distinctive:
            return item["id"]
    return ""


# A question about the occasion written as plain text (the model should use ask_occasion, but sometimes
# just writes the question). The buttons are then added under it, so the customer always gets them.
_ASKS_OCCASION = re.compile(r"(?i)[^.!?؟\n]*\boccasion\b[^.!?؟\n]*\?|[^.!؟?\n]*مناسب[ةه][^.!؟?\n]*[؟?]")


def product_mentioned(texts: list) -> bool:
    return any(_PRODUCT_WORDS.search(t) for t in texts)


def occasion_given(texts: list) -> str:
    """The words in the customer's recent messages that already give the occasion ('' if none)."""
    for text in reversed(texts):
        match = _OCCASION_HINTS.search(text)
        if match:
            return match.group(0).strip()
    return ""


def clean_names(text: str) -> str:
    return _KUNYA_EN.sub("", _KUNYA.sub("", text))


# The chat page already shows the shop's welcome, so a welcome at the start of the agent's reply is
# removed (the instructions say so, but the model sometimes still opens with one). If the customer
# greeted first, greeting back is polite, so nothing is removed then.
_OPENING_WELCOME = re.compile(
    r"^\s*(?:(?:welcome|hello|hi|hey)(?: to [^.!،,]{1,40})?(?:, it is a pleasure to have you)?(?:[!.]|,? and )|"
    r"it is a pleasure to have you[!.]|"
    r"(?:حياك الله|حيّاك الله|يا هلا|هلا والله|أهلاً وسهلاً|أهلا وسهلا|اهلا وسهلا|أهلاً|أهلا|مرحباً|مرحبا)"
    r"(?: بك| فيك| بكم)?(?: في [^،.!]{1,40})?[،,.!]|"
    r"أسعدتنا زيارتك[،,.!])\s*", re.IGNORECASE)
_CUSTOMER_GREETING = re.compile(r"(?i)\b(hi|hello|hey|salam|salaam|good (morning|evening))\b|السلام|مرحب|هلا|أهلا|اهلا|صباح|مساء")


def strip_opening_welcome(text: str) -> str:
    for _ in range(3):  # e.g. "حياك الله، أسعدتنا زيارتك. ..."
        new = _OPENING_WELCOME.sub("", text, count=1)
        if new == text:
            break
        text = new
    return text[:1].upper() + text[1:] if text[:1].isascii() else text


class OpeningTrim:
    """Holds back the first words of a streamed reply until it can see whether they are a welcome."""

    def __init__(self):
        self.buffer, self.done = "", False

    def feed(self, piece: str) -> str:
        if self.done:
            return piece
        self.buffer += piece
        if len(self.buffer) < 60 and not re.search(r"[.!؟?]\s|\n", self.buffer):
            return ""
        return self.flush()

    def flush(self) -> str:
        if self.done:
            return ""
        self.done = True
        return strip_opening_welcome(self.buffer)


# Notes the model sometimes writes to itself ("Customer wants a ring... Search the catalog.").
# Everything in a reply is shown to the customer, so a paragraph that starts like this is hidden.
_SELF_NOTE = re.compile(
    r"(?i)^\s*(?:(?:the )?customer(?:'s)? (?:wants|asked|asks|is|has|said|says|needs|chose|mentioned|already)|"
    r"(?:let me|i will|i'll|i need to|i should|now i|next,? i) (?:search|check|call|use|look up|find|show)|"
    r"العميل |الزبون |سأبحث|دعني أبحث)|"
    r"search_catalog|show_products|ask_occasion|show_contact_sales|show_request_form|show_booking_form|"
    r"get_price_quote|get_gold_rates|occasion is known|tool result")


# Opening sentences that only delay the answer: saying what cannot be shared (breakdown, making charge,
# gold price per gram) or "Let me get it for you". The answer that follows is kept.
_FILLER = re.compile(
    r"(?i)^\s*(?:(?:i'?m sorry,? |sorry,? |unfortunately,? )?(?:i can'?t|i cannot|i am not able to|we can'?t|we cannot|"
    r"we don'?t)[^.!?\n]{0,60}(?:breakdown|making|labou?r|gold price|per gram|price per)|"
    r"let me (?:get|check|find|look|pull|fetch)|one moment|just a moment|"
    r"(?:لا أستطيع|لا استطيع|ما نقدر|ما أقدر|ما اقدر|للأسف|عذراً|عذرا)[^.!؟\n]{0,60}"
    r"(?:تفصيل|أفصل|افصل|نفصل|نفصّل|أفصّل|المصنعية|مصنعية|سعر الجرام|سعر جرام|سعر الذهب)|"
    r"دعني (?:أتحقق|اتحقق|أبحث|ابحث)|لحظة)[^.!?؟\n]*(?:[.!?؟]+\s*|\n|$)")


def _filler_length(text: str) -> int:
    """Length of an opening filler sentence in text (0 if none). A sentence with a number (a price) is kept."""
    m = _FILLER.match(text)
    return m.end() if m and m.end() > 0 and not re.search(r"\d", m.group(0)) else 0


def strip_fillers(text: str) -> str:
    while cut := _filler_length(text):
        print(f"  [dropped filler] {text[:cut].strip()}")
        text = text[cut:]
    return text


class BlockFilter:
    """Looks at the start of each streamed paragraph: hides the whole paragraph if it is a self-note,
    and drops opening filler sentences (see _FILLER)."""

    def __init__(self):
        self.buffer, self.decided, self.hide = "", False, False

    def feed(self, piece: str) -> str:
        if self.decided:
            return "" if self.hide else piece
        self.buffer += piece
        while True:
            end = re.search(r"[.!؟?]\s|\n", self.buffer)
            if end is None:
                return "" if len(self.buffer) < 80 else self.flush()
            if _SELF_NOTE.search(self.buffer) or not _filler_length(self.buffer[:end.end()]):
                return self.flush()
            print(f"  [dropped filler] {self.buffer[:end.end()].strip()}")
            self.buffer = self.buffer[end.end():]  # look at the next sentence

    def flush(self) -> str:
        if self.decided:
            return ""
        self.decided = True
        self.hide = bool(_SELF_NOTE.search(self.buffer))
        if self.hide:
            print(f"  [hidden self-note] {self.buffer[:120]}")
            return ""
        return strip_fillers(self.buffer)  # e.g. a last sentence with no space after it


def _mostly_repeats(new: str, said: str) -> bool:
    """True when most words of `new` were already said in this reply."""
    words = set(re.findall(r"\w{3,}", new.lower()))
    return bool(words) and len(words & set(re.findall(r"\w{3,}", said.lower()))) / len(words) >= 0.5


class NameFilter:
    """Cleans streamed text. It holds back the last word (and a title word just before it)
    until the next word arrives, so a title split across pieces is still caught."""

    def __init__(self):
        self.pending = ""

    def feed(self, piece: str) -> str:
        self.pending += piece
        cut = max(self.pending.rfind(" "), self.pending.rfind("\n"))
        if cut <= 0:
            return ""
        head = self.pending[:cut]
        words = head.split()
        if words and words[-1] in _KUNYA_WORDS:  # wait to see the word after the title
            cut = head.rfind(words[-1])
            head = self.pending[:cut]
        self.pending = self.pending[cut:]
        return clean_names(head)

    def flush(self) -> str:
        out, self.pending = clean_names(self.pending), ""
        return out


def _shop_section(shop_id: str) -> str:
    s = shop_tools.load_settings(shop_id)
    policies = "\n".join(f"- {k}: {v}" for k, v in s["policies"].items())
    return f"""
# The shop you work for
Shop name: {s['shop_name_ar']} / {s['shop_name_en']}
City: {s['city']}
Address: {s['address']}
Phone: {s['phone']}
Opening hours: {s['opening_hours']}
Approved payment options: {', '.join(s['payment_options'])}
Maximum labor-fee discount you may give: {s['max_labor_discount_percent']}%
Free extras you may offer during negotiation: {', '.join(s['free_extras'])}
High-value threshold (hand over to a human above this total): {s['high_value_threshold_sar']} SAR
Human sales team response time: {s['human_reply_time']}

Policies:
{policies}

Prices change with the gold price. Always get them from the tools, never from this text.
"""


CATALOG_IN_PROMPT_MAX = 150  # small catalogs are given to Claude up front, so showing pieces takes one step


def _catalog_section(shop_id: str) -> str:
    """The shop's pieces (no prices), so Claude can show them directly without searching first."""
    items = shop_tools.load_catalog(shop_id)
    if len(items) > CATALOG_IN_PROMPT_MAX:
        return ""
    lines = []
    for i in items:
        state = "" if i.get("in_stock", 0) > 0 else " | SOLD OUT"
        lines.append(f"{i['id']} | {i['category']} | {i['karat']}K | {i['weight_g']} g | {i['name_en']} / "
                     f"{i['name_ar']} | {i.get('short_en', '')}{state}")
    return ("\n# The shop's pieces (no prices here)\n"
            "You may show pieces from this list directly with show_products, using their ids. Use search_catalog "
            "for a price limit or when you are not sure. Never state a price from memory; prices come only from "
            "the tools and the cards.\n" + "\n".join(lines) + "\n")


class Agent:
    def __init__(self):
        # If Claude goes quiet for 45 s (it happens rarely), give up and try again instead of
        # leaving the customer waiting for minutes.
        self.client = anthropic.Anthropic(timeout=45.0, max_retries=2)
        self._prompts: dict[str, tuple[float, str]] = {}   # shop_id -> (settings mtime, system prompt)
        self._sessions: dict[str, list] = {}               # session_id -> message history
        self._languages: dict[str, str] = {}               # session_id -> language chosen in the chat window
        self._price_locks: dict[str, dict] = {}            # session_id -> gold price locked for this chat
        self._customer_texts: dict[str, list] = {}         # session_id -> what the customer typed (recent first-hand)
        self._chosen: dict[str, str] = {}                  # session_id -> the last piece the customer tapped "Choose" on
        self._labels: dict[str, tuple] = {}                # session_id -> shop name from a demo link (Arabic, English)
        self._forms: dict[str, dict] = {}                  # form_id -> form shown to a customer
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _system_prompt(self, shop_id: str) -> str:
        # Rebuilt only when the shop edits settings.json or catalog.json, so the prompt cache stays warm.
        mtime = (shop_tools.settings_mtime(shop_id), (shop_tools.SHOPS_DIR / shop_id / "catalog.json").stat().st_mtime)
        cached = self._prompts.get(shop_id)
        if cached is None or cached[0] != mtime:
            prompt = SALES_METHOD + "\n" + _shop_section(shop_id) + _catalog_section(shop_id) + CHAT_FORMAT_RULES
            self._prompts[shop_id] = (mtime, prompt)
        return self._prompts[shop_id][1]

    def _system_blocks(self, shop_id: str) -> list:
        # 1-hour cache: the shop's instructions stay ready between quiet periods (cheap with Haiku).
        return [{"type": "text", "text": self._system_prompt(shop_id),
                 "cache_control": {"type": "ephemeral", "ttl": "1h"}}]

    def warm_up(self, shop_id: str) -> None:
        """Prepare the shop's instructions on Anthropic's side (max_tokens 0: nothing is generated),
        so the first real question is answered faster."""
        self.client.beta.messages.create(
            model=MODEL, max_tokens=0, system=self._system_blocks(shop_id), tools=shop_tools.TOOL_DEFINITIONS,
            output_config={"effort": EFFORT}, messages=[{"role": "user", "content": "warmup"}], **FALLBACK_OPTIONS)

    def _lock_for(self, key: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(key, threading.Lock())

    def reply(self, shop_id: str, session_id: str, user_text: str, language: str, chosen_item: dict | None = None,
              staff_note: str = "", shop_label: tuple = ("", "")):
        """Answer one customer message. Yields events for the chat page as they happen:
        {"type": "text", "text": ...}       a piece of the reply (streamed word by word)
        {"type": "block_end"}               one message bubble is finished
        {"type": "message", "text": ...}    a whole message (shown with cards or the form)
        {"type": "products", "items": [...]} product cards with picture and price
        {"type": "form", ...}               the contact form or the visit booking form
        {"type": "choices", "options": [...]} tap buttons (the occasion question)
        {"type": "contact_sales", ...}      a "Contact sales" button (opens a chat with the shop's sales team)
        {"type": "retry"}                   drop the unfinished bubble, the answer restarts
        {"type": "retract"}                 remove the text shown so far in this reply (it led to a blocked step)
        """
        key = f"{shop_id}:{session_id}"
        with self._lock_for(key):
            history = self._sessions.setdefault(key, [])
            # What a staff member and the customer said while the AI was paused (live chat).
            content = [{"type": "text", "text": staff_note}] if staff_note else []
            if any(shop_label) and self._labels.get(key) != shop_label:
                # A demo link with a prospect's shop name: the chat uses that name instead of the demo shop's.
                self._labels[key] = shop_label
                name_ar, name_en = shop_label[0] or shop_label[1], shop_label[1] or shop_label[0]
                content.append({"type": "text", "text": f"(Chat window note: in this chat the shop is called "
                                f"\"{name_ar}\" in Arabic and \"{name_en}\" in English. Use this name, not the name "
                                "in your instructions. Everything else about the shop stays the same.)"})
            if self._languages.get(key) != language:
                # Tell Claude about the language button, in the first message and whenever it changes.
                self._languages[key] = language
                name = "Arabic" if language == "ar" else "English"
                content.append({"type": "text", "text": f"(Chat window note: the customer chose {name}. "
                                f"Reply in {name}, unless the customer writes in another language.)"})
            if chosen_item:
                self._chosen[key] = chosen_item["id"]
                # The customer tapped "Choose" on a product card; buttons for the next steps appear by themselves.
                content.append({"type": "text", "text":
                    f"(Chat window note: the customer tapped 'Choose' on piece {chosen_item['id']} "
                    f"({chosen_item['name_en']}). Reply in 1-2 short sentences: confirm the choice warmly and mention "
                    "anything important, like the size or weight note. Do not show products or ask the occasion. "
                    "Buttons for Buy on website, Reserve, Share and Show similar appear under your reply.)"})
            content.append({"type": "text", "text": user_text})
            history.append({"role": "user", "content": content})
            self._customer_texts.setdefault(key, []).append(user_text)
            yield from self._run(shop_id, key, history)

    def contact_form(self, shop_id: str, session_id: str, request: str, language: str) -> dict:
        """The contact form, for "Contact sales" when the shop has no WhatsApp sales number."""
        return self._new_form(f"{shop_id}:{session_id}", "show_request_form", {
            "reason": "talk_to_sales", "request_text": request[:300],
            "summary_for_team": f"Customer asked to contact sales about: {request[:300]}", "language": language})

    def reserve_form(self, shop_id: str, session_id: str, item: dict, language: str) -> dict:
        """The contact form for "Reserve / talk to sales", made by code (no AI call), pre-filled with the piece."""
        key = f"{shop_id}:{session_id}"
        with self._lock_for(key):
            price_lock = self._price_locks.setdefault(key, {})
            card = shop_tools.product_cards(shop_id, [item["id"]], language, price_lock)[0]
        price = f"{card['total_sar']:,.2f} SAR" if card["total_sar"] is not None else "price on request"
        request = (f"حجز: {item['name_ar']} ({item['id']})" if language == "ar"
                   else f"Reserve: {item['name_en']} ({item['id']})")
        summary = (f"Customer chose {item['id']} {item['name_en']} and wants to reserve or buy it. "
                   f"Price in the chat: {price}. {item.get('weight_note', '')}").strip()
        return self._new_form(key, "show_request_form", {
            "reason": "reserve", "request_text": request, "summary_for_team": summary, "language": language})

    def get_form(self, shop_id: str, session_id: str, form_id: str) -> dict | None:
        form = self._forms.get(form_id)
        return form if form and form["session_key"] == f"{shop_id}:{session_id}" and not form["sent"] else None

    def form_sent(self, shop_id: str, session_id: str, form_id: str, name: str, ref: str, visit: str = "") -> None:
        """Record in the chat that the customer sent the form, so the agent knows next time."""
        key = f"{shop_id}:{session_id}"
        with self._lock_for(key):
            self._forms[form_id]["sent"] = True
            if self._forms[form_id]["kind"] == "booking":
                note = (f"(Chat window note: {name} sent the visit booking form for {visit}, reference {ref}. "
                        "The shop team will call to confirm. Do not ask for these details again.)")
            else:
                note = (f"(Chat window note: {name} sent the contact form, reference {ref}. The sales team has it. "
                        "Do not ask for contact details again.)")
            self._sessions.setdefault(key, []).append({"role": "user", "content": [{"type": "text", "text": note}]})

    def _new_form(self, session_key: str, tool_name: str, tool_input: dict) -> dict:
        """A contact form (show_request_form) or a visit booking form (show_booking_form)."""
        form_id = uuid.uuid4().hex
        kind = "booking" if tool_name == "show_booking_form" else "request"
        # reason, occasion and summary_for_team stay on the server; the page only gets what it shows.
        prefill = tool_input["notes"] if kind == "booking" else tool_input["request_text"]
        self._forms[form_id] = {"session_key": session_key, "sent": False, "kind": kind,
                                "reason": tool_input.get("reason", "book_visit"),
                                "occasion": tool_input.get("occasion", ""),
                                "summary_for_team": tool_input["summary_for_team"]}
        return {"type": "form", "kind": kind, "form_id": form_id, "request_text": prefill,
                "language": tool_input["language"]}

    def _stream_once(self, system: list, history: list, turn: dict):
        """One call to Claude, streamed. Yields text events, then returns the full message.
        turn["trim"]: still at the start of the reply (an opening welcome is removed);
        turn["said"]: the text already shown in this reply."""
        for attempt in range(3):
            try:
                with self.client.beta.messages.stream(
                    model=MODEL,
                    max_tokens=16000,
                    system=system,
                    tools=shop_tools.TOOL_DEFINITIONS,
                    messages=history,
                    output_config={"effort": EFFORT},
                    **FALLBACK_OPTIONS,
                ) as stream:
                    names = NameFilter()
                    opening = OpeningTrim() if turn["trim"] else None
                    block = BlockFilter()
                    for event in stream:
                        if event.type == "text":
                            piece = block.feed(event.text)
                            piece = opening.feed(piece) if opening and piece else piece
                            if out := names.feed(piece) if piece else "":
                                turn["said"].append(out)
                                yield {"type": "text", "text": out}
                        elif event.type == "content_block_stop" and event.content_block.type == "text":
                            rest = block.flush()
                            block = BlockFilter()  # the next paragraph is checked on its own
                            rest = (opening.feed(rest) if rest else "") + opening.flush() if opening else rest
                            out = (names.feed(rest) if rest else "") + names.flush()
                            if out:
                                turn["said"].append(out)
                                yield {"type": "text", "text": out}
                            if "".join(turn["said"]).strip():
                                turn["trim"], opening = False, None
                            yield {"type": "block_end"}
                    return stream.get_final_message()
            except ValueError:
                # A tool input arrived as broken JSON. Ask again (API errors are not ValueError).
                if attempt == 2:
                    raise
                yield {"type": "retry"}

    def _run(self, shop_id: str, session_key: str, history: list):
        system = self._system_blocks(shop_id)
        price_lock = self._price_locks.setdefault(session_key, {})
        latest = (self._customer_texts.get(session_key) or [""])[-1]
        turn = {"trim": not _CUSTOMER_GREETING.search(latest), "said": []}

        for _ in range(MAX_TOOL_ROUNDS):
            said_before = len(turn["said"])
            response = yield from self._stream_once(system, history, turn)

            if response.stop_reason == "refusal":
                text = ("عذرًا، ما أقدر أساعد في هذا الطلب. فريق المبيعات يسعده يخدمك.\n"
                        "Sorry, I cannot help with this request. Our sales team will be happy to help you.")
                history.append({"role": "assistant", "content": [{"type": "text", "text": text}]})
                yield {"type": "retry"}
                yield {"type": "text", "text": text}
                return

            # Keep the full response (thinking + tool calls), never only the text.
            history.append({"role": "assistant", "content": response.content})

            tool_calls = [b for b in response.content if b.type == "tool_use"]
            if not tool_calls:
                if not turn.get("choices") and _ASKS_OCCASION.search("".join(turn["said"])):
                    print("  [occasion buttons added under a text question]")
                    yield {"type": "choices", "options": shop_tools.occasion_choices(
                        shop_id, self._languages.get(session_key, "ar"))}
                return

            results, shown = [], []
            # If this step already showed text, the tool's own message would repeat it.
            # (Text that was hidden, like a note the model wrote to itself, does not count.)
            wrote_text = bool("".join(turn["said"][said_before:]).strip())
            for call in tool_calls:
                recent = self._customer_texts.get(session_key, [])[-2:]
                given = occasion_given(recent)
                # A price/details question about a named (or already chosen) piece: no occasion needed.
                about_piece = bool(_PRICE_OR_DETAIL.search(latest)) and bool(
                    named_piece(shop_id, latest) or self._chosen.get(session_key))
                if call.name == "show_products" and about_piece and not call.input.get("occasion", "").strip():
                    call.input["occasion"] = "not needed: question about the price or details of a named piece"
                if response.stop_reason == "max_tokens":
                    output, is_error = '{"error": "The tool input was cut off. Please try again."}', True
                elif call.name == "ask_occasion" and given:
                    output, is_error = json.dumps({"error": f"The customer already gave the occasion or purpose "
                                                            f"('{given}'). Do not ask it. Search and show pieces."},
                                                  ensure_ascii=False), True
                elif call.name == "ask_occasion" and about_piece:
                    output, is_error = json.dumps({"error": "The customer only asked about the price or details of a "
                                                            "piece they named. Do not ask the occasion; just answer "
                                                            "(you may show that piece with show_products)."}), True
                elif call.name == "ask_occasion" and not product_mentioned(recent):
                    output, is_error = json.dumps({"error": "The customer has not asked about a product yet. Do not "
                                                            "ask the occasion now; just answer their message."}), True
                else:
                    output, is_error = shop_tools.run_tool(shop_id, call.name, call.input, price_lock)
                if call.name == "ask_occasion" and is_error and wrote_text:
                    # The text that led up to the blocked question is taken back on the page; otherwise
                    # the next answer would repeat it.
                    yield {"type": "retract"}
                    turn["said"].clear()
                    turn["trim"] = not _CUSTOMER_GREETING.search(latest)
                    wrote_text = False
                print(f"  [tool] {call.name}({call.input}) -> {output[:200]}")
                if call.name in shop_tools.DISPLAY_TOOLS and not is_error:
                    # The message travels inside the tool input, so it reaches the customer word for word.
                    message = strip_fillers(clean_names(call.input["message"].strip()))
                    if turn["trim"]:
                        message = strip_opening_welcome(message)
                    said_so_far = " ".join(turn["said"] + [e["text"] for e in shown if e["type"] == "message"])
                    already_said = (call.name == "show_contact_sales" and any(e["type"] == "message" for e in shown)) \
                        or _mostly_repeats(message, said_so_far)
                    if message and not wrote_text and not already_said:
                        shown.append({"type": "message", "text": message})
                        turn["trim"] = False
                    if call.name == "show_products":
                        shown.append({"type": "products", "items": shop_tools.product_cards(
                            shop_id, call.input["item_ids"], call.input["language"], price_lock)})
                    elif call.name == "ask_occasion":
                        turn["choices"] = True
                        shown.append({"type": "choices",
                                      "options": shop_tools.occasion_choices(shop_id, call.input["language"])})
                    elif call.name == "show_contact_sales":
                        shown.append({"type": "contact_sales", "request": call.input["request"],
                                      "language": call.input["language"]})
                    else:
                        shown.append(self._new_form(session_key, call.name, call.input))
                results.append({"type": "tool_result", "tool_use_id": call.id,
                                "content": output, "is_error": is_error})
            history.append({"role": "user", "content": results})

            yield from shown
            # Showing cards or the form ends the reply: no extra call to Claude, so the answer
            # is faster. The customer's next message is added after these tool results.
            if all(c.name in shop_tools.DISPLAY_TOOLS for c in tool_calls) and not any(r["is_error"] for r in results):
                said = " ".join(turn["said"] + [e["text"] for e in shown if e["type"] == "message"])
                if not turn.get("choices") and _ASKS_OCCASION.search(said):
                    yield {"type": "choices", "options": shop_tools.occasion_choices(
                        shop_id, self._languages.get(session_key, "ar"))}
                return

        text = "عذرًا، صار عندي تأخير. ممكن تعيد سؤالك؟\nSorry, this took too long. Can you ask again?"
        history.append({"role": "assistant", "content": [{"type": "text", "text": text}]})
        yield {"type": "text", "text": text}
