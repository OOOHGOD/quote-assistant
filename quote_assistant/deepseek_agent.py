from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any

import requests

from .models import field, issue


DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v4-flash"
CHAT_PATH = "/chat/completions"


class DeepSeekAgentError(RuntimeError):
    """Raised when DeepSeek cannot return a valid quote extraction."""


@dataclass(frozen=True)
class DeepSeekSettings:
    api_key: str
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    timeout_seconds: float = 120.0

    @classmethod
    def from_env(cls) -> "DeepSeekSettings":
        api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        if not api_key:
            raise DeepSeekAgentError("DEEPSEEK_API_KEY is required for DeepSeek extraction.")
        return cls(
            api_key=api_key,
            base_url=os.environ.get("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL,
            model=os.environ.get("DEEPSEEK_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL,
            timeout_seconds=float(os.environ.get("DEEPSEEK_TIMEOUT_SECONDS", "120")),
        )


class DeepSeekClient:
    def __init__(self, settings: DeepSeekSettings):
        self.settings = settings

    def complete_json(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        url = f"{self.settings.base_url.rstrip('/')}{CHAT_PATH}"
        payload = {
            "model": self.settings.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
            "response_format": {"type": "json_object"},
        }
        response = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {self.settings.api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=self.settings.timeout_seconds,
        )
        try:
            body = response.json()
        except ValueError as exc:
            raise DeepSeekAgentError(f"DeepSeek returned non-JSON response: HTTP {response.status_code}") from exc
        if response.status_code >= 400:
            raise DeepSeekAgentError(f"DeepSeek request failed: HTTP {response.status_code}, payload={body}")
        content = (((body.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
        return parse_json_object(content)


class QuoteExtractionAgent:
    def __init__(self, client: DeepSeekClient):
        self.client = client

    def extract_quote(self, markdown_text: str, *, source_name: str, ocr_job_id: str = "") -> dict[str, Any]:
        payload = self.client.complete_json(
            system_prompt=QUOTE_SYSTEM_PROMPT,
            user_prompt=build_quote_prompt(markdown_text, source_name=source_name),
        )
        return quote_from_agent_payload(payload, source_name=source_name, ocr_job_id=ocr_job_id)


QUOTE_SYSTEM_PROMPT = """
You are a deterministic quotation extraction agent.
Return only one JSON object. Do not include markdown fences.
If a value is missing, use null. Never infer supplier location or commercial values.
Use numbers for quantity, unit_price, amount, subtotal, tax, and grand_total.
""".strip()


def build_quote_prompt(markdown_text: str, *, source_name: str) -> str:
    return f"""
Extract a supplier quotation from this local OCR document.

Required JSON shape:
{{
  "headers": {{
    "quote_no": null,
    "supplier": null,
    "customer": null,
    "project": null,
    "quote_date": null,
    "currency": null
  }},
  "items": [
    {{
      "product_code": null,
      "product_name": null,
      "specification": null,
      "material": null,
      "color": null,
      "unit": null,
      "quantity": null,
      "unit_price": null,
      "amount": null,
      "location": null,
      "remarks": null
    }}
  ],
  "totals": {{
    "subtotal": null,
    "tax": null,
    "grand_total": null
  }},
  "notes": []
}}

Source file: {source_name}

OCR markdown:
{markdown_text}
""".strip()


HEADER_FIELDS = ("quote_no", "supplier", "customer", "project", "quote_date", "currency")
ITEM_FIELDS = (
    "product_code",
    "product_name",
    "specification",
    "material",
    "color",
    "unit",
    "quantity",
    "unit_price",
    "amount",
    "location",
    "remarks",
)
TOTAL_FIELDS = ("subtotal", "tax", "grand_total")
NUMERIC_FIELDS = {"quantity", "unit_price", "amount", "subtotal", "tax", "grand_total"}


def parse_json_object(content: str) -> dict[str, Any]:
    if not content:
        raise DeepSeekAgentError("DeepSeek returned empty content.")
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, flags=re.DOTALL)
    candidate = fenced.group(1) if fenced else content
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise DeepSeekAgentError(f"DeepSeek returned invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise DeepSeekAgentError("DeepSeek JSON response must be an object.")
    return payload


def quote_from_agent_payload(payload: dict[str, Any], *, source_name: str, ocr_job_id: str = "") -> dict[str, Any]:
    headers = payload.get("headers") if isinstance(payload.get("headers"), dict) else {}
    totals = payload.get("totals") if isinstance(payload.get("totals"), dict) else {}
    raw_items = payload.get("items") if isinstance(payload.get("items"), list) else []
    source = {"type": "deepseek_agent", "source_file": source_name, "ocr_job_id": ocr_job_id}

    quote_items = []
    for index, raw_item in enumerate(raw_items, start=1):
        if not isinstance(raw_item, dict):
            continue
        item = {"line_no": index}
        for name in ITEM_FIELDS:
            value = normalize_value(raw_item.get(name), numeric=name in NUMERIC_FIELDS)
            item[name] = field(value, confidence_for(value), source)
        quote_items.append(item)

    extraction_issues = []
    notes = payload.get("notes")
    if isinstance(notes, list):
        for note in notes:
            if note:
                extraction_issues.append(issue("AGENT_NOTE", "warning", str(note), "agent.notes"))

    return {
        "document": {
            "page_count": None,
            "text_char_count": None,
            "parser": "paddleocr+deepseek-agent-v1",
        },
        "headers": {
            name: field(normalize_value(headers.get(name)), confidence_for(headers.get(name)), source)
            for name in HEADER_FIELDS
        },
        "items": quote_items,
        "totals": {
            name: field(
                normalize_value(totals.get(name), numeric=name in NUMERIC_FIELDS),
                confidence_for(totals.get(name)),
                source,
            )
            for name in TOTAL_FIELDS
        },
        "evidence": [source],
        "extraction_issues": extraction_issues,
        "raw_pages": [],
    }


def normalize_value(value: Any, *, numeric: bool = False) -> Any:
    if value in ("", "N/A", "n/a", "null", None):
        return None
    if not numeric:
        return str(value).strip()
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = re.sub(r"[^0-9.\-]", "", str(value))
    if cleaned in {"", ".", "-", "-."}:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def confidence_for(value: Any) -> float:
    return 0.92 if value not in (None, "") else 0.0
