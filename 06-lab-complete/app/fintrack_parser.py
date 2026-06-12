import re
import json
from typing import Any

from app.fintrack_models import ParseMeta, ParsedTransaction


INCOME_HINTS = ("nhan", "lương", "luong", "thuong", "thưởng", "thu nhập", "thu nhap")

CATEGORY_RULES = {
    "xang": "transport",
    "xăng": "transport",
    "grab": "transport",
    "an": "food",
    "ăn": "food",
    "com": "food",
    "cafe": "food",
    "cf": "food",
    "luong": "salary",
    "lương": "salary",
    "thuong": "bonus",
    "thưởng": "bonus",
    "freelance": "freelance",
    "nha": "housing",
    "nhà": "housing",
    "dien": "utilities",
    "điện": "utilities",
    "nuoc": "utilities",
    "nước": "utilities",
}


def _normalize(text: str) -> str:
    return " ".join(text.strip().lower().split())


def _parse_amount_vnd(text: str) -> float | None:
    compact = text.lower().replace(",", ".")
    pattern = re.compile(r"(\d+(?:\.\d+)?)\s*(k|nghin|nghìn|tr|trieu|triệu|m|)\b")
    matches = pattern.findall(compact)
    if not matches:
        return None

    value_str, unit = matches[0]
    value = float(value_str)
    if unit in ("k", "nghin", "nghìn"):
        return value * 1_000
    if unit in ("tr", "trieu", "triệu", "m"):
        return value * 1_000_000
    return value


def _extract_wallet_name(text: str) -> str | None:
    m = re.search(r"(?:tu|từ)\s+vi\s+([a-zA-Z0-9À-ỹà-ỹ\s]+)$", text, flags=re.IGNORECASE)
    if m:
        return m.group(1).strip()

    m2 = re.search(r"vi\s+([a-zA-Z0-9À-ỹà-ỹ\s]+)$", text, flags=re.IGNORECASE)
    if m2:
        return m2.group(1).strip()
    return None


def _infer_type(normalized_text: str) -> str:
    for hint in INCOME_HINTS:
        if hint in normalized_text:
            return "income"
    return "expense"


def _infer_category(normalized_text: str, tx_type: str) -> str:
    for keyword, category in CATEGORY_RULES.items():
        if keyword in normalized_text:
            return category
    return "other_income" if tx_type == "income" else "other_expense"


def _deterministic_parse(raw_text: str, default_currency: str = "VND") -> ParsedTransaction:
    normalized = _normalize(raw_text)
    amount = _parse_amount_vnd(normalized)
    tx_type = _infer_type(normalized)
    category = _infer_category(normalized, tx_type)
    wallet_name = _extract_wallet_name(raw_text)

    notes: list[str] = []
    needs_clarification = False

    if amount is None:
        amount = 0.0
        needs_clarification = True
        notes.append("Khong tim thay so tien ro rang.")

    if not wallet_name:
        notes.append("Khong tim thay ten vi, se dung vi mac dinh.")

    confidence = 0.98
    if wallet_name is None:
        confidence -= 0.08
    if needs_clarification:
        confidence = 0.45

    description = raw_text.strip()
    meta = ParseMeta(
        confidence=max(0.0, min(confidence, 1.0)),
        needs_clarification=needs_clarification,
        notes=notes,
    )
    return ParsedTransaction(
        type=tx_type,  # type: ignore[arg-type]
        amount=amount,
        currency=default_currency,
        wallet_name=wallet_name,
        category=category,
        description=description,
        raw_text=raw_text,
        meta=meta,
    )


def _safe_string(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _openai_parse(raw_text: str, openai_api_key: str, model: str, default_currency: str = "VND") -> ParsedTransaction:
    from openai import OpenAI

    client = OpenAI(api_key=openai_api_key)
    system_prompt = (
        "You are a financial transaction parser for a Vietnamese personal finance app. "
        "Extract one transaction from user text and return valid JSON only with fields: "
        "type (expense|income), amount (number), currency, wallet_name (string|null), category, description. "
        "Use currency VND unless user explicitly gives another currency."
    )
    user_prompt = (
        f"User text: {raw_text}\n"
        "Return JSON only. No markdown. If uncertain, still make best guess and include wallet_name as null."
    )
    response = client.chat.completions.create(
        model=model,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )
    content = response.choices[0].message.content
    if not content:
        raise ValueError("OpenAI returned empty response.")
    payload = json.loads(content)

    tx_type = _safe_string(payload.get("type", "expense")).lower()
    if tx_type not in ("expense", "income"):
        tx_type = "expense"

    amount_raw = payload.get("amount", 0)
    try:
        amount = float(amount_raw)
    except (TypeError, ValueError):
        amount = 0.0

    wallet_value = payload.get("wallet_name")
    wallet_name = _safe_string(wallet_value) if wallet_value is not None else None
    category = _safe_string(payload.get("category", "")) or ("other_income" if tx_type == "income" else "other_expense")
    description = _safe_string(payload.get("description", raw_text)) or raw_text
    currency = _safe_string(payload.get("currency", default_currency)).upper() or default_currency

    notes: list[str] = ["Parsed by OpenAI model."]
    needs_clarification = amount <= 0
    if needs_clarification:
        notes.append("Model khong tra ve amount hop le.")
    if wallet_name in (None, ""):
        notes.append("Khong tim thay ten vi, se dung vi mac dinh.")
        wallet_name = None

    return ParsedTransaction(
        type=tx_type,  # type: ignore[arg-type]
        amount=amount,
        currency=currency,
        wallet_name=wallet_name,
        category=category,
        description=description,
        raw_text=raw_text,
        meta=ParseMeta(
            confidence=0.95 if not needs_clarification else 0.4,
            needs_clarification=needs_clarification,
            notes=notes,
        ),
    )


def parse_financial_text(
    raw_text: str,
    default_currency: str = "VND",
    openai_api_key: str = "",
    llm_model: str = "gpt-4o-mini",
) -> ParsedTransaction:
    if openai_api_key:
        try:
            return _openai_parse(
                raw_text=raw_text,
                openai_api_key=openai_api_key,
                model=llm_model,
                default_currency=default_currency,
            )
        except Exception:
            parsed = _deterministic_parse(raw_text, default_currency=default_currency)
            parsed.meta.notes.append("OpenAI parse failed, fallback to deterministic parser.")
            return parsed
    return _deterministic_parse(raw_text, default_currency=default_currency)

