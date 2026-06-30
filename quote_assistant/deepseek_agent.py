"""DeepSeek 报价单抽取 agent。

PaddleOCR 负责把 PDF 转成可读 Markdown，本模块负责把 Markdown 抽取成项目统一的 quote JSON。
统一 JSON 后，后续校验、人工审核和 Excel 映射都不再依赖 OCR 原始格式。
"""

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
    """DeepSeek API 配置。

    API key 只从环境变量读取；模型和 base_url 可通过环境变量覆盖，方便切换官方或兼容服务。
    """

    api_key: str
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    timeout_seconds: float = 120.0

    @classmethod
    def from_env(cls) -> "DeepSeekSettings":
        """从环境变量读取 agent 运行配置。"""
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
    """DeepSeek Chat Completions 的最小 JSON 客户端。"""

    def __init__(self, settings: DeepSeekSettings):
        self.settings = settings

    def complete_json(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        """请求模型返回 JSON object，并解析成 Python 字典。"""
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
    """把 OCR Markdown 转为标准报价单结构的应用层 agent。"""

    def __init__(self, client: DeepSeekClient):
        self.client = client

    def extract_quote(self, markdown_text: str, *, source_name: str, ocr_job_id: str = "") -> dict[str, Any]:
        """执行抽取并把模型返回值规范化为内部 quote schema。"""
        payload = self.client.complete_json(
            system_prompt=QUOTE_SYSTEM_PROMPT,
            user_prompt=build_quote_prompt(markdown_text, source_name=source_name),
        )
        return quote_from_agent_payload(payload, source_name=source_name, ocr_job_id=ocr_job_id, ocr_markdown=markdown_text)


QUOTE_SYSTEM_PROMPT = """
You are a deterministic quotation extraction agent.
Return only one JSON object. Do not include markdown fences.
If a value is missing, use null. Never infer supplier location or commercial values.
Use numbers for quantity, unit_price, amount, subtotal, tax, and grand_total.
""".strip()


def build_quote_prompt(markdown_text: str, *, source_name: str) -> str:
    """构造抽取提示词。

    提示词明确要求缺失值用 null，避免模型为了“补全表格”而推断供应商地点或商业金额。
    """
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
    """从模型内容中解析 JSON。

    正常情况下 DeepSeek 会按 response_format 返回纯 JSON；这里保留 fenced code block 兼容，
    方便调试或切换模型时仍能处理包在 ```json 里的内容。
    """
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


def quote_from_agent_payload(
    payload: dict[str, Any],
    *,
    source_name: str,
    ocr_job_id: str = "",
    ocr_markdown: str = "",
) -> dict[str, Any]:
    """把 agent 原始 JSON 转成系统内部 quote schema。

    每个字段都包装成 `{value, confidence, source}`，这样校验和前端审核可以统一处理来源与置信度。
    """
    headers = payload.get("headers") if isinstance(payload.get("headers"), dict) else {}
    totals = payload.get("totals") if isinstance(payload.get("totals"), dict) else {}
    raw_items = payload.get("items") if isinstance(payload.get("items"), list) else []
    base_source = {"type": "deepseek_agent", "source_file": source_name, "ocr_job_id": ocr_job_id}

    quote_items = []
    for index, raw_item in enumerate(raw_items, start=1):
        if not isinstance(raw_item, dict):
            continue
        item = {"line_no": index}
        for name in ITEM_FIELDS:
            value = normalize_value(raw_item.get(name), numeric=name in NUMERIC_FIELDS)
            confidence, confidence_detail = confidence_for(value, name=name, ocr_markdown=ocr_markdown)
            item[name] = field(value, confidence, source_with_confidence(base_source, confidence_detail))
        quote_items.append(item)

    extraction_issues = []
    notes = payload.get("notes")
    if isinstance(notes, list):
        for note in notes:
            if note:
                extraction_issues.append(issue("AGENT_NOTE", "warning", str(note), "agent.notes"))

    quote = {
        "document": {
            "page_count": None,
            "text_char_count": None,
            "parser": "paddleocr+deepseek-agent-v1",
        },
        "headers": {
            name: field(
                normalize_value(headers.get(name)),
                *field_confidence_and_source(headers.get(name), name, base_source, ocr_markdown),
            )
            for name in HEADER_FIELDS
        },
        "items": quote_items,
        "totals": {
            name: field(
                normalize_value(totals.get(name), numeric=name in NUMERIC_FIELDS),
                *field_confidence_and_source(totals.get(name), name, base_source, ocr_markdown),
            )
            for name in TOTAL_FIELDS
        },
        "evidence": [base_source],
        "extraction_issues": extraction_issues,
        "raw_pages": [],
    }
    apply_consistency_confidence(quote)
    return quote


def normalize_value(value: Any, *, numeric: bool = False) -> Any:
    """清洗模型字段值；数值字段会去掉币种符号和千分位后转 float。"""
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


def field_confidence_and_source(
    raw_value: Any,
    name: str,
    base_source: dict[str, Any],
    ocr_markdown: str,
) -> tuple[float, dict[str, Any]]:
    """Return a confidence/source pair for a normalized field wrapper."""
    value = normalize_value(raw_value, numeric=name in NUMERIC_FIELDS)
    confidence, detail = confidence_for(value, name=name, ocr_markdown=ocr_markdown)
    return confidence, source_with_confidence(base_source, detail)


def confidence_for(value: Any, *, name: str = "", ocr_markdown: str = "") -> tuple[float, dict[str, Any]]:
    """Score an agent field using OCR evidence instead of a fixed non-empty value.

    The score is a business confidence used for review routing. It is not a raw
    PaddleOCR probability or an LLM probability.
    """
    if value in (None, ""):
        return 0.0, {"method": "missing_value", "evidence": "missing"}

    numeric = name in NUMERIC_FIELDS or isinstance(value, (int, float))
    base = 0.84 if numeric else 0.82
    evidence = "agent_only"
    if ocr_value_supported(value, ocr_markdown, numeric=numeric):
        base += 0.08 if numeric else 0.1
        evidence = "ocr_text_match"
    elif numeric:
        base -= 0.08
        evidence = "no_numeric_ocr_match"

    if name in {"quote_no", "currency", "quantity", "unit_price", "amount", "subtotal", "tax", "grand_total"}:
        base += 0.02
    if name in {"product_name", "supplier"}:
        base += 0.01

    return clamp_confidence(base), {"method": "ocr_evidence_and_business_rules", "evidence": evidence}


def source_with_confidence(base_source: dict[str, Any], detail: dict[str, Any]) -> dict[str, Any]:
    """Copy the shared source and attach field-specific confidence evidence."""
    return {**base_source, "confidence_detail": detail}


def ocr_value_supported(value: Any, ocr_markdown: str, *, numeric: bool) -> bool:
    """Check whether a field value has direct textual evidence in OCR markdown."""
    if not ocr_markdown:
        return False
    if numeric:
        number = normalize_value(value, numeric=True)
        if number is None:
            return False
        return any(candidate and candidate in ocr_markdown for candidate in numeric_candidates(float(number)))
    needle = compact_text(str(value))
    haystack = compact_text(ocr_markdown)
    return bool(needle and needle in haystack)


def compact_text(value: str) -> str:
    """Normalize text for OCR evidence matching across spaces and punctuation."""
    return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", value).lower()


def numeric_candidates(value: float) -> list[str]:
    """Generate common OCR text forms for a numeric value."""
    candidates = {str(value), f"{value:.2f}"}
    if value.is_integer():
        integer = int(value)
        candidates.add(str(integer))
        candidates.add(f"{integer}.00")
    return sorted(candidates, key=len, reverse=True)


def apply_consistency_confidence(quote: dict[str, Any]) -> None:
    """Adjust numeric confidence with quote arithmetic checks."""
    subtotal_expected = 0.0
    all_item_amounts_consistent = True
    for index, item in enumerate(quote.get("items") or []):
        quantity = field_value(item.get("quantity"))
        unit_price = field_value(item.get("unit_price"))
        amount = field_value(item.get("amount"))
        if all(isinstance(candidate, (int, float)) for candidate in (quantity, unit_price, amount)):
            expected = round(quantity * unit_price, 2)
            subtotal_expected += expected
            if near_equal(expected, amount):
                boost_fields(item, ("quantity", "unit_price", "amount"), 0.03, "quantity_unit_price_amount_match")
            else:
                all_item_amounts_consistent = False
                cap_fields(item, ("amount",), 0.62, "quantity_unit_price_amount_mismatch")
        else:
            all_item_amounts_consistent = False

    totals = quote.get("totals") or {}
    subtotal = field_value(totals.get("subtotal"))
    tax = field_value(totals.get("tax"))
    grand_total = field_value(totals.get("grand_total"))
    if isinstance(subtotal, (int, float)) and all_item_amounts_consistent and near_equal(subtotal, subtotal_expected):
        boost_fields(totals, ("subtotal",), 0.04, "subtotal_matches_items")
    elif isinstance(subtotal, (int, float)):
        cap_fields(totals, ("subtotal",), 0.62, "subtotal_mismatch_or_incomplete_items")

    if all(isinstance(candidate, (int, float)) for candidate in (subtotal, tax, grand_total)):
        if near_equal(round(subtotal + tax, 2), grand_total):
            boost_fields(totals, ("tax", "grand_total"), 0.04, "grand_total_matches_subtotal_plus_tax")
        else:
            cap_fields(totals, ("grand_total",), 0.62, "grand_total_mismatch")
    elif isinstance(grand_total, (int, float)) and isinstance(subtotal, (int, float)) and tax is None:
        if near_equal(subtotal, grand_total):
            boost_fields(totals, ("grand_total",), 0.02, "grand_total_matches_subtotal_without_tax")


def field_value(candidate: dict[str, Any] | None) -> Any:
    """Read the value from a field wrapper."""
    return (candidate or {}).get("value")


def boost_fields(container: dict[str, Any], names: tuple[str, ...], amount: float, reason: str) -> None:
    """Increase field confidence and record the business-rule evidence."""
    for name in names:
        candidate = container.get(name)
        if isinstance(candidate, dict):
            candidate["confidence"] = clamp_confidence(float(candidate.get("confidence") or 0.0) + amount)
            add_confidence_rule(candidate, reason)


def cap_fields(container: dict[str, Any], names: tuple[str, ...], cap: float, reason: str) -> None:
    """Cap field confidence when business rules contradict the extracted value."""
    for name in names:
        candidate = container.get(name)
        if isinstance(candidate, dict):
            candidate["confidence"] = min(float(candidate.get("confidence") or 0.0), cap)
            add_confidence_rule(candidate, reason)


def add_confidence_rule(candidate: dict[str, Any], reason: str) -> None:
    """Append business-rule evidence to a field's source metadata."""
    source = candidate.setdefault("source", {})
    detail = source.setdefault("confidence_detail", {})
    rules = detail.setdefault("business_rules", [])
    if reason not in rules:
        rules.append(reason)


def near_equal(left: float, right: float, tolerance: float = 0.02) -> bool:
    """Compare money-like values with a small relative tolerance."""
    return abs(left - right) <= max(0.01, abs(left) * tolerance)


def clamp_confidence(value: float) -> float:
    """Clamp business confidence to the public 0..1 scale."""
    return round(max(0.0, min(0.99, value)), 3)
