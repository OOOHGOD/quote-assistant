# 字段置信度计算说明

本项目中的字段置信度是业务审核置信度，不是 PaddleOCR 原始字符概率，也不是 DeepSeek 的模型概率。

## 评分来源

DeepSeek 把 OCR Markdown 抽取成结构化 quote JSON 后，系统会为每个字段计算置信度：

```text
字段值是否缺失
+ 字段值是否能在 OCR Markdown 中找到文本证据
+ 数字字段是否能在 OCR 文本中找到对应数字
+ 数量 x 单价 是否等于金额
+ 明细合计 是否等于小计
+ 小计 + 税额 是否等于总计
+ 是否经过人工审核修正
```

## 基本规则

- 缺失字段：`0%`
- 只有 DeepSeek 给出结果，但 OCR 文本中找不到直接证据：较低置信度，通常会进入人工关注范围。
- OCR 文本中能找到对应字段值：提高置信度。
- 数量、单价、金额自洽：进一步提高相关数字字段置信度。
- 金额公式不一致：限制相关金额字段置信度，并由校验层生成阻断问题。
- 人工审核修正：`100%`，并记录 `corrected_by_human=true`。

## 审核阈值

阈值来自 `config.json`：

```json
{
  "confidence_threshold": 0.8,
  "critical_confidence_threshold": 0.9
}
```

关键字段低于 `critical_confidence_threshold` 会产生阻断级低置信度问题。普通字段低于 `confidence_threshold` 会产生 warning。

## 字段来源记录

每个字段的 `source.confidence_detail` 会记录简要证据：

```json
{
  "method": "ocr_evidence_and_business_rules",
  "evidence": "ocr_text_match",
  "business_rules": ["quantity_unit_price_amount_match"]
}
```

这让前端和人工审核可以区分：

- 字段来自 OCR 文本证据；
- 字段只是 agent 结构化推断；
- 字段是否通过金额规则校验；
- 字段是否由人工确认。
