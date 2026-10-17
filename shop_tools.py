"""The shop's tools: the actions the agent can take.

Each shop lives in its own folder under shops/<shop_id>/ with:
  settings.json  - name, hours, gold rates, policies, discount limits
  catalog.json   - the pieces for sale
  data/          - leads and appointments the agent saves (created automatically)

Claude never calculates prices itself. It calls these functions, and the
numbers come from the shop's own files.
"""

import json
import re
import threading
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

import gold_rates

SHOPS_DIR = Path(__file__).parent / "shops"
SHOP_ID_PATTERN = re.compile(r"^[a-z0-9-]{1,50}$")
IMAGE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,80}\.(jpg|jpeg|png|webp|svg)$")
_write_lock = threading.Lock()


# ---------- loading shop files ----------

def shop_exists(shop_id: str) -> bool:
    return bool(SHOP_ID_PATTERN.match(shop_id)) and (SHOPS_DIR / shop_id / "settings.json").is_file()


def load_settings(shop_id: str) -> dict:
    return json.loads((SHOPS_DIR / shop_id / "settings.json").read_text(encoding="utf-8"))


def load_catalog(shop_id: str) -> list:
    return json.loads((SHOPS_DIR / shop_id / "catalog.json").read_text(encoding="utf-8"))


def settings_mtime(shop_id: str) -> float:
    return (SHOPS_DIR / shop_id / "settings.json").stat().st_mtime


def image_path(shop_id: str, file_name: str) -> Path | None:
    """Path of a product picture, or None if the name is not allowed or the file is missing."""
    if not shop_exists(shop_id) or not IMAGE_NAME_PATTERN.match(file_name):
        return None
    path = SHOPS_DIR / shop_id / "images" / file_name
    return path if path.is_file() else None


def _save_record(shop_id: str, kind: str, record: dict) -> None:
    data_dir = SHOPS_DIR / shop_id / "data"
    data_dir.mkdir(exist_ok=True)
    with _write_lock, open(data_dir / f"{kind}.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ---------- price calculation ----------

RATE_UNAVAILABLE = ("The live gold price is not available right now, so no price can be given. "
                    "Say so honestly and offer show_contact_sales.")


def pricing_settings(shop_id: str, price_lock: dict | None = None) -> dict:
    """The shop's settings with the gold rates filled in.

    gold_rate_source "auto": live world price + the shop's gold_markup_percent.
      price_lock is the chat's own dict: the price is fetched once per chat and kept.
    gold_rate_source "manual": the rates typed in settings.json.
    Raises gold_rates.GoldRateUnavailable when no recent live price exists.
    """
    settings = load_settings(shop_id)
    if settings.get("gold_rate_source") == "auto":
        if gold_rates.is_configured():
            live = gold_rates.shop_rates(settings.get("gold_markup_percent", 0), price_lock)
            settings["gold_rates_sar_per_gram"] = live["rates"]
            settings["gold_rates_updated_at"] = live["updated_at"]
        else:
            settings["gold_rates_updated_at"] = ("SAMPLE RATES - the live gold price is not set up yet "
                                                 "(GOLDAPI_KEY missing in .env)")
    return settings


def _quote(item: dict, settings: dict, labor_discount_percent: float = 0) -> dict:
    rate = settings["gold_rates_sar_per_gram"][item["karat"]]
    gold_value = item["weight_g"] * rate
    labor_full = item["weight_g"] * item["labor_fee_sar_per_g"]
    labor = labor_full * (1 - labor_discount_percent / 100)
    stones = item.get("stones_sar", 0)
    subtotal = gold_value + labor + stones
    vat_rate = 0 if item.get("vat_exempt") else settings["vat_rate"]
    vat = subtotal * vat_rate
    quote = {
        "item_id": item["id"],
        "karat": item["karat"],
        "weight_g": item["weight_g"],
        "gold_rate_sar_per_g": rate,
        "gold_value_sar": round(gold_value, 2),
        "labor_fee_sar": round(labor, 2),
        "stones_sar": round(stones, 2),
        "subtotal_before_vat_sar": round(subtotal, 2),
        "vat_rate": vat_rate,
        "vat_sar": round(vat, 2),
        "total_sar": round(subtotal + vat, 2),
        "gold_rates_updated_at": settings.get("gold_rates_updated_at", ""),
    }
    if labor_discount_percent:
        quote["labor_discount_percent"] = labor_discount_percent
        quote["labor_fee_before_discount_sar"] = round(labor_full, 2)
    if item.get("vat_exempt"):
        quote["vat_note"] = "VAT not applied to this investment-grade item, per the shop's settings."
    if item.get("weight_note"):
        quote["weight_note"] = item["weight_note"]
    return quote


# ---------- the tools Claude can call ----------

# There is no tool for the gold price per gram: the shop does not share it with customers
# (with a piece's weight it would show the making charge). Customers see final prices only.

def _item_words(item: dict) -> list:
    text = " ".join([item["name_en"], item["name_ar"], item["category"], " ".join(item.get("tags", []))]).lower()
    return re.findall(r"\w+", text)


def _word_matches(word: str, item_words: list) -> bool:
    """English: the item word starts with the search word ("ring" finds "rings" but not "earrings").
    Arabic: either word contains the other, so "الخواتم" still finds "خواتم"."""
    if word.isascii():
        return any(w.startswith(word) for w in item_words)
    return any(word in w or (len(w) > 2 and w in word) for w in item_words)


def search_catalog(shop_id: str, text: str, category: str, karat: str, max_total_sar: float,
                   price_lock: dict | None = None) -> dict:
    try:
        settings = pricing_settings(shop_id, price_lock)
    except gold_rates.GoldRateUnavailable:
        settings = None  # pieces are listed without prices
    items = load_catalog(shop_id)
    if category != "any":
        items = [i for i in items if i["category"] == category]
    if karat != "any":
        items = [i for i in items if i["karat"] == karat]

    words = [w for w in re.findall(r"\w+", text.lower()) if len(w) > 1]
    note = ""
    sold_out = []
    if words:
        # Best matches first: more of the customer's words found, and found in the name, ranks higher.
        def score(item):
            name_words = re.findall(r"\w+", f"{item['name_en']} {item['name_ar']}".lower())
            all_words = _item_words(item)
            return sum(2 if _word_matches(w, name_words) else 1 for w in words if _word_matches(w, all_words))
        matched = sorted((i for i in items if score(i) > 0), key=score, reverse=True)
        sold_out = [i for i in matched if i.get("in_stock", 0) <= 0]
        if any(i.get("in_stock", 0) > 0 for i in matched):
            items = matched
        elif sold_out:
            note = ("The pieces that match are sold out (see sold_out_matches). Say so in one sentence, show what "
                    "the shop has of the same type with show_products (search by category if needed), then call "
                    "show_contact_sales.")
        else:
            note = ("The shop does not have this. Say so in one sentence, show what the shop has of the same type "
                    "with show_products (search by category if needed), then call show_contact_sales. If the shop "
                    "has nothing of that type, only call show_contact_sales.")

    priced = [(i, _quote(i, settings) if settings else {"total_sar": None})
              for i in items if i.get("in_stock", 0) > 0]
    if max_total_sar > 0 and settings:
        priced = [(i, q) for i, q in priced if q["total_sar"] <= max_total_sar]
    if settings is None:
        note = (note + " " + RATE_UNAVAILABLE).strip()

    results = [
        {
            "id": i["id"],
            "name_en": i["name_en"],
            "name_ar": i["name_ar"],
            "short_en": i.get("short_en", ""),
            "category": i["category"],
            "karat": i["karat"],
            "weight_g": i["weight_g"],
            "stones_note": i.get("stones_note", ""),
            "weight_note": i.get("weight_note", ""),
            "available": True,  # no stock counts: they invite false "last piece" urgency
            "total_price_sar": q["total_sar"],
        }
        for i, q in priced[:6]
    ]
    return {
        "results": results,
        "sold_out_matches": [{"id": i["id"], "name_en": i["name_en"], "name_ar": i["name_ar"]} for i in sold_out],
        "note": note or ("No pieces found." if not results else ""),
        "gold_rates_updated_at": settings.get("gold_rates_updated_at", "") if settings else "",
    }


def get_price_quote(shop_id: str, item_id: str, labor_discount_percent: float,
                    price_lock: dict | None = None) -> dict:
    try:
        settings = pricing_settings(shop_id, price_lock)
    except gold_rates.GoldRateUnavailable:
        return {"error": RATE_UNAVAILABLE}
    item =next((i for i in load_catalog(shop_id) if i["id"] == item_id), None)
    if item is None:
        return {"error": f"No item with id {item_id}. Use search_catalog to find the right id."}
    max_discount = settings["max_labor_discount_percent"]
    if labor_discount_percent < 0 or labor_discount_percent > max_discount:
        return {
            "error": f"Labor discount must be between 0 and {max_discount}%. "
                     "For more, offer show_request_form with reason talk_to_sales."
        }
    # The shop gives customers the total only, never the parts (gold value, making charge, VAT amount),
    # so the AI does not get the parts either.
    q = _quote(item, settings, labor_discount_percent)
    quote = {"item_id": item["id"], "karat": item["karat"], "weight_g": item["weight_g"],
             "total_sar": q["total_sar"],
             "price_includes": "gold, making charge and stones" + ("" if item.get("vat_exempt") else ", plus 15% VAT"),
             "gold_rates_updated_at": q["gold_rates_updated_at"],
             "note": "Tell the customer the total price only. Do not give a price breakdown."}
    if labor_discount_percent:
        quote["labor_discount_percent"] = labor_discount_percent
        quote["total_before_discount_sar"] = _quote(item, settings)["total_sar"]
    for key in ("vat_note", "weight_note"):
        if key in q:
            quote[key] = q[key]
    return quote


def _site_link(url: str, lang: str) -> str:
    """The shop's website in the customer's language: '/en/' in the link becomes '/ar/' for Arabic."""
    return url.replace("/en/", "/ar/", 1) if lang == "ar" else url


def product_cards(shop_id: str, item_ids: list, language: str, price_lock: dict | None = None) -> list:
    """Cards the chat page shows: picture, name, karat, weight, price, short description.
    Prices are calculated here, never written by Claude. Without a live gold price, total_sar is None."""
    try:
        settings = pricing_settings(shop_id, price_lock)
    except gold_rates.GoldRateUnavailable:
        settings = None
    shop = settings or load_settings(shop_id)
    catalog = {i["id"]: i for i in load_catalog(shop_id)}
    lang = "ar" if language == "ar" else "en"
    cards = []
    for item_id in item_ids[:4]:
        item = catalog.get(item_id)
        if item is None:
            continue
        cards.append({
            "id": item["id"],
            "name": item[f"name_{lang}"],
            "description": item.get(f"short_{lang}", ""),
            "karat": item["karat"],
            "weight_g": item["weight_g"],
            "total_sar": _quote(item, settings)["total_sar"] if settings else None,
            "image": f"/shop-images/{shop_id}/{item['image']}" if item.get("image") else "",
            # The product on the shop's own website; a demo shop without a website gets the app's own product page.
            "url": _site_link(item["url"], lang) if item.get("url") else
                   (f"/store/{shop_id}/{item_id}?lang={lang}" if shop.get("demo_store") else ""),
            "sample_image": bool(shop.get("sample_images")),
            "language": lang,
        })
    return cards


def occasion_choices(shop_id: str, language: str) -> list:
    """The occasion buttons the customer can tap, in their language."""
    lang = "ar" if language == "ar" else "en"
    return [o[lang] for o in load_settings(shop_id).get("occasions", [])]


def ask_occasion(shop_id: str, message: str, language: str) -> dict:
    return {
        "status": "shown",
        "note": "The customer now sees your question with buttons: "
                + ", ".join(occasion_choices(shop_id, "en"))
                + ". They may also type their own answer. Wait for the answer.",
    }


def show_products(shop_id: str, message: str, occasion: str, item_ids: list, language: str) -> dict:
    if not occasion.strip():
        # The shop wants the occasion known before pieces are shown.
        return {"error": "You do not know the occasion yet. Call ask_occasion first, then show pieces "
                         "that fit the occasion."}
    catalog_ids = {i["id"] for i in load_catalog(shop_id)}
    unknown = [i for i in item_ids if i not in catalog_ids]
    if unknown:
        return {"error": f"Unknown item ids: {unknown}. Use ids from search_catalog."}
    return {
        "status": "shown",
        "note": "The customer now sees a card for each piece with picture, name, karat, weight, total price "
                "and a short description. Do not repeat these details.",
    }


def show_contact_sales(shop_id: str, message: str, request: str, language: str) -> dict:
    return {
        "status": "shown",
        "note": "The customer now sees a 'Contact sales' button. When someone from the sales team is online, a "
                "person can join this chat; otherwise it opens WhatsApp with the sales team (or the contact form "
                "if the shop has no WhatsApp number). Do not promise how fast a person will answer.",
    }


def show_booking_form(shop_id: str, message: str, occasion: str, notes: str, summary_for_team: str,
                      language: str) -> dict:
    # The form is shown by the chat page; the details go straight to save_booking(), never through Claude.
    return {
        "status": "form_shown",
        "note": "The customer now sees a short booking form (name, mobile number, day, time, notes, consent). "
                "Do not ask for these details in the chat. When the customer sends the form, "
                "the shop team gets it and you will see a note.",
    }


def visit_slots(settings: dict, day: date) -> list:
    """Times a customer can choose for a visit on that day (Friday has its own list)."""
    if day.weekday() == 4:  # Friday
        return settings.get("visit_slots_friday", [])
    return settings.get("visit_slots", [])


def open_visit_slots(settings: dict, day: date) -> list:
    """Visit times still possible on that day: for today, only times at least one hour from now (Riyadh)."""
    slots = visit_slots(settings, day)
    now = datetime.now(gold_rates.RIYADH)
    if day == now.date():
        soon = (now + timedelta(hours=1)).strftime("%H:%M")
        slots = [s for s in slots if s >= soon]
    return slots


def check_visit_time(settings: dict, day_text: str, time_text: str) -> date | None:
    """The visit day if day and time are allowed: today up to 30 days ahead, at an opening time not yet passed."""
    try:
        day = date.fromisoformat(day_text)
    except ValueError:
        return None
    today = datetime.now(gold_rates.RIYADH).date()
    if not today <= day <= today + timedelta(days=30) or time_text not in open_visit_slots(settings, day):
        return None
    return day


def _reference(prefix: str) -> str:
    """A reference like REQ-1015-1642-4F2A9C: date, time and a random part, so two requests never share one."""
    return f"{prefix}-{datetime.now().strftime('%m%d-%H%M')}-{uuid.uuid4().hex[:6].upper()}"


def save_booking(shop_id: str, form: dict, name: str, phone: str, day: str, time_text: str,
                 notes: str, language: str) -> str:
    """Save a visit booking the customer sent through the form. Returns the reference number."""
    ref = _reference("APT")
    _save_record(shop_id, "appointments", {
        "ref": ref, "created_at": datetime.now().isoformat(timespec="seconds"),
        "customer_name": name, "phone": phone, "visit_date": day, "visit_time": time_text,
        "occasion": form["occasion"], "customer_notes": notes, "agent_summary": form["summary_for_team"],
        "language": language, "consent_to_contact": True,
    })
    print(f"\n*** NEW VISIT BOOKING for shop '{shop_id}' ({ref}): {day} {time_text} - {name} {phone}\n"
          f"    Notes: {notes}\n    Agent summary: {form['summary_for_team']}\n")
    return ref


def show_request_form(shop_id: str, message: str, reason: str, request_text: str, summary_for_team: str,
                      language: str) -> dict:
    # The form itself is shown by the chat page; the customer's details go straight
    # to save_request(), never through Claude.
    return {
        "status": "form_shown",
        "note": "The customer now sees a short form (name, mobile number, request, consent). "
                "Do not ask for these details in the chat. When the customer sends the form, "
                "the sales team gets it and you will see a note.",
    }


PHONE_PATTERN = re.compile(r"^\+?[0-9][0-9 -]{7,18}$")


def save_request(shop_id: str, form: dict, name: str, phone: str, request_text: str, language: str) -> str:
    """Save a request the customer sent through the form. Returns the reference number."""
    ref = _reference("REQ")
    record = {
        "ref": ref, "created_at": datetime.now().isoformat(timespec="seconds"),
        "reason": form["reason"], "customer_name": name, "phone": phone,
        "customer_request": request_text, "agent_summary": form["summary_for_team"],
        "language": language, "consent_to_contact": True,
    }
    _save_record(shop_id, "requests", record)
    print(f"\n*** NEW REQUEST for shop '{shop_id}' ({ref}): {form['reason']} - {name} {phone}\n"
          f"    Customer: {request_text}\n    Agent summary: {form['summary_for_team']}\n")
    return ref


# ---------- tool definitions sent to Claude ----------

def _schema(properties: dict) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


TOOL_DEFINITIONS = [
    {
        "name": "search_catalog",
        "description": "Search the shop's pieces. Returns up to 6 pieces in stock with their total price, plus "
                       "sold_out_matches (pieces the shop sells that are sold out now). Only recommend pieces in "
                       "the results.",
        "strict": True,
        "input_schema": _schema({
            "text": {"type": "string", "description": "Keywords in Arabic or English, e.g. 'shabka', 'هدية مولود'. Empty string for no keyword filter."},
            "category": {"type": "string", "enum": ["any", "set", "ring", "necklace", "bracelet", "earrings", "bar"]},
            "karat": {"type": "string", "enum": ["any", "18", "21", "22", "24"]},
            "max_total_sar": {"type": "number", "description": "Highest total price in SAR including VAT. 0 means no limit."},
        }),
    },
    {
        "name": "show_products",
        "description": "Show product cards in the chat: picture, name, karat, weight, total price, and a short "
                       "description. Use it every time you recommend or the customer asks about specific pieces "
                       "(1 to 3 pieces). It is the last step of your reply.",
        "strict": True,
        "input_schema": _schema({
            "message": {"type": "string", "description": "Your message to the customer, shown above the cards. "
                        "1-2 short sentences, e.g. why the piece fits. Do not repeat it as normal text."},
            "occasion": {"type": "string", "description": "The occasion the customer told you, in English, e.g. "
                         "'engagement', 'gift for mother', 'for myself', 'just looking'. Empty string if you do "
                         "not know it yet. Never guess."},
            "item_ids": {"type": "array", "items": {"type": "string"}, "description": "Piece ids from search_catalog, e.g. ['D-101']."},
            "language": {"type": "string", "enum": ["ar", "en"], "description": "The language the customer is using."},
        }),
    },
    {
        "name": "show_contact_sales",
        "description": "Show a 'Contact sales' button that opens a chat with the shop's sales team. Use it when "
                       "the shop does not have exactly what the customer wants (sold out, not sold here, a "
                       "specific design, size or karat), after showing what the shop has of the same type with "
                       "show_products in the same step. Also use it when the customer asks to talk to a person. "
                       "It is the last step of your reply.",
        "strict": True,
        "input_schema": _schema({
            "message": {"type": "string", "description": "A short line above the button, e.g. 'Our sales team "
                        "can help you find it.' Empty string if you already said it."},
            "request": {"type": "string", "description": "What the customer is looking for, short, in their "
                        "language, e.g. 'خلخال ذهب عيار 21'. It goes into the message to the sales team."},
            "language": {"type": "string", "enum": ["ar", "en"], "description": "The language the customer is using."},
        }),
    },
    {
        "name": "ask_occasion",
        "description": "Ask the customer the occasion, with tap buttons (engagement, wedding, new baby, Eid, "
                       "graduation, gift, for myself, just looking). Use it only when the customer asks about or "
                       "for a product and NO occasion or purpose is known yet. Before calling it, read the "
                       "customer's messages: if they name an occasion or who it is for (e.g. 'for my daughter's "
                       "graduation', 'for my wife's birthday', 'for my son's engagement', 'for Eid', 'for myself'), "
                       "do NOT call it; search the catalog instead. Never use it for questions like the gold "
                       "price. It is the last step of your reply.",
        "strict": True,
        "input_schema": _schema({
            "message": {"type": "string", "description": "Your question to the customer, shown above the buttons. "
                        "1-2 short, warm sentences, e.g. 'A bracelet is a lovely choice. What is the occasion?' "
                        "Do not repeat it as normal text."},
            "language": {"type": "string", "enum": ["ar", "en"], "description": "The language the customer is using."},
        }),
    },
    {
        "name": "get_price_quote",
        "description": "Get the total price of one piece (including VAT). Use it for every price you tell the "
                       "customer. Set labor_discount_percent to 0 unless you are offering a labor-fee discount "
                       "during negotiation.",
        "strict": True,
        "input_schema": _schema({
            "item_id": {"type": "string", "description": "The piece id from search_catalog, e.g. D-101."},
            "labor_discount_percent": {"type": "number", "description": "Labor-fee discount in percent. 0 for the normal price."},
        }),
    },
    {
        "name": "show_booking_form",
        "description": "Show a short form where the customer books a private visit to the shop: name, mobile "
                       "number, day, time, and notes. Use it when the customer wants to visit or agrees to your "
                       "invitation. Never ask for these details in the chat; this form does it. It is the last "
                       "step of your reply.",
        "strict": True,
        "input_schema": _schema({
            "message": {"type": "string", "description": "Your message to the customer, shown above the form. "
                        "1-2 short sentences. Do not repeat it as normal text."},
            "occasion": {"type": "string", "description": "The occasion, e.g. 'shabka'. Empty string if unknown."},
            "notes": {"type": "string", "description": "Pieces of interest in the customer's language, short. "
                      "It fills the notes field; the customer can change it. Empty string if none."},
            "summary_for_team": {"type": "string", "description": "Short note for the shop team in English: who "
                                 "is coming, occasion, budget, pieces and prices discussed."},
            "language": {"type": "string", "enum": ["ar", "en"], "description": "The language the customer is using."},
        }),
    },
    {
        "name": "show_request_form",
        "description": "Show a short form in the chat where the customer leaves name, mobile number, and their "
                       "request, so the shop's team can call them back. Use it for: a custom order, a discount "
                       "above the limit, a high-value piece, a complaint or return, or when the customer asks to "
                       "be called back. When the shop simply does not have what they want, use show_products + "
                       "show_contact_sales instead. Never ask for name or phone in the chat; this form does it. "
                       "It is the last step of your reply. You may call show_products in the same step to show "
                       "similar pieces.",
        "strict": True,
        "input_schema": _schema({
            "message": {"type": "string", "description": "Your message to the customer, shown above the form. "
                        "1-2 short sentences: say honestly what happened and what happens next. "
                        "Do not repeat it as normal text."},
            "reason": {"type": "string", "enum": [
                "sold_out", "not_available", "custom_order", "discount_above_limit",
                "high_value", "complaint_or_return", "talk_to_sales", "unsure",
            ]},
            "request_text": {"type": "string", "description": "The customer's request in their own language, short, "
                             "e.g. '21K gold anklet'. It fills the request field; the customer can change it."},
            "summary_for_team": {"type": "string", "description": "Short note for the sales team in English: what "
                                 "the customer wants, occasion, budget, pieces and prices discussed."},
            "language": {"type": "string", "enum": ["ar", "en"], "description": "The language the customer is using."},
        }),
    },
]

# Tools that only show something in the chat window. When a reply ends with
# them, no extra call to Claude is needed.
DISPLAY_TOOLS = {"show_products", "show_request_form", "show_booking_form", "ask_occasion", "show_contact_sales"}

# Replies are streamed, so tool inputs stream too. The API then does not check
# them, so validate_input() below checks every input before a tool runs.
for _tool in TOOL_DEFINITIONS:
    _tool["eager_input_streaming"] = True

TOOL_FUNCTIONS = {
    "search_catalog": search_catalog,
    "show_products": show_products,
    "ask_occasion": ask_occasion,
    "show_contact_sales": show_contact_sales,
    "get_price_quote": get_price_quote,
    "show_booking_form": show_booking_form,
    "show_request_form": show_request_form,
}


_SCHEMAS = {t["name"]: t["input_schema"] for t in TOOL_DEFINITIONS}
_TYPES = {"string": str, "number": (int, float), "array": list}


def validate_input(name: str, tool_input) -> str | None:
    """Return a problem description, or None if the input matches the tool's schema."""
    schema = _SCHEMAS.get(name)
    if schema is None:
        return f"Unknown tool {name}"
    if not isinstance(tool_input, dict):
        return "Input must be an object"
    props = schema["properties"]
    if set(tool_input) != set(props):
        return f"Expected exactly these fields: {sorted(props)}"
    for key, spec in props.items():
        value = tool_input[key]
        if isinstance(value, bool) or not isinstance(value, _TYPES[spec["type"]]):
            return f"Field {key} must be a {spec['type']}"
        if "enum" in spec and value not in spec["enum"]:
            return f"Field {key} must be one of {spec['enum']}"
        if spec["type"] == "array" and not all(isinstance(v, str) for v in value):
            return f"Field {key} must be a list of strings"
    return None


PRICED_TOOLS = {"search_catalog", "get_price_quote"}


def run_tool(shop_id: str, name: str, tool_input, price_lock: dict | None = None) -> tuple[str, bool]:
    """Run one tool. Returns (result as JSON text, is_error).
    price_lock is the chat's locked gold price, used by the tools that give prices."""
    problem = validate_input(name, tool_input)
    if problem:
        return json.dumps({"INVALID_JSON": json.dumps(tool_input, ensure_ascii=False), "problem": problem},
                          ensure_ascii=False), True
    func = TOOL_FUNCTIONS[name]
    try:
        extra = {"price_lock": price_lock} if name in PRICED_TOOLS else {}
        result = func(shop_id, **tool_input, **extra)
    except Exception as e:  # report the problem to Claude instead of crashing the chat
        return json.dumps({"error": f"{type(e).__name__}: {e}"}), True
    return json.dumps(result, ensure_ascii=False), "error" in result
