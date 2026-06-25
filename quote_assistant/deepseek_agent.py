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
        return quote_from_agent_payload(payload, source_name=source_name, ocr_job_id=ocr_job_id)


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


def quote_from_agent_payload(payload: dict[str, Any], *, source_name: str, ocr_job_id: str = "") -> dict[str, Any]:
    """把 agent 原始 JSON 转成系统内部 quote schema。

    每个字段都包装成 `{value, confidence, source}`，这样校验和前端审核可以统一处理来源与置信度。
    """
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


def confidence_for(value: Any) -> float:
    """给 agent 结果设置默认置信度；缺失值置信度为 0。"""
    return 0.92 if value not in (None, "") else 0.0
