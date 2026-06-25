const state = { jobs: [], current: null, original: null, templateReady: false, templateReason: "", templateSetup: null, acceptance: null };
const headerLabels = {
  quote_no: "报价编号", supplier: "供应商", customer: "客户", project: "项目",
  quote_date: "报价日期", currency: "币种"
};
const totalLabels = { subtotal: "subtotal", tax: "tax", grand_total: "grand_total" };
const mappingLabels = {
  "quote.headers.quote_no.value": "报价编号", "quote.headers.supplier.value": "供应商",
  "quote.headers.customer.value": "客户", "quote.headers.project.value": "项目",
  "quote.headers.quote_date.value": "报价日期", "quote.headers.currency.value": "币种",
  "line_no": "序号列", "product_code.value": "产品编码列", "product_name.value": "产品名称列",
  "specification.value": "规格尺寸列", "material.value": "材质列", "color.value": "颜色列",
  "unit.value": "单位列", "quantity.value": "数量列", "unit_price.value": "单价列",
  "amount.value": "金额列", "location.value": "房间/区域列", "remarks.value": "备注列",
  "quote.totals.subtotal.value": "subtotal", "quote.totals.tax.value": "tax", "quote.totals.grand_total.value": "grand_total"
};
const itemColumns = [
  ["product_code", "product_code"], ["product_name", "product_name"], ["specification", "specification"],
  ["material", "material"], ["color", "color"], ["unit", "unit"], ["quantity", "quantity"],
  ["unit_price", "unit_price"], ["amount", "amount"], ["location", "location"], ["remarks", "remarks"]
];

const $ = (selector) => document.querySelector(selector);
function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"]/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[char]));
}
function showToast(message) {
  const toast = $("#toast"); toast.textContent = message; toast.hidden = false;
  window.setTimeout(() => { toast.hidden = true; }, 3200);
}
async function api(url, options = {}) {
  const response = await fetch(url, options);
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json") ? await response.json() : await response.blob();
  if (!response.ok) throw new Error(payload.error || `璇锋眰澶辫触锛?{response.status}`);
  return payload;
}
function downloadJson(filename, payload) {
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}
function formatStatus(status) {
  return { needs_review: "异常待处理", ready_for_review: "待人工审核", approved: "已批准", rejected: "已驳回" }[status] || status;
}
async function loadQueue(selectId = null) {
  const health = await api("/api/health");
  state.acceptance = await api("/api/acceptance");
  state.templateReady = Boolean(health.excel_template?.configured);
  state.templateReason = health.excel_template?.reason || "";
  const templateStatus = $("#templateStatus");
  templateStatus.textContent = state.templateReady
    ? `妯℃澘鏍煎紡宸查攣瀹氾細${health.excel_template.template_file}`
    : "Excel模板未就绪，且格式不允许修改";
  templateStatus.title = state.templateReady
    ? `SHA-256: ${health.excel_template.template_sha256}；仅允许向固定单元格写值，不修改Excel格式。`
    : state.templateReason;
  templateStatus.classList.toggle("ready", state.templateReady);
  state.templateSetup = await api("/api/template/setup");
  $("#mappingButton").hidden = !state.templateSetup.available || state.templateReady;
  state.jobs = await api("/api/jobs");
  const queue = $("#queue");
  queue.innerHTML = state.jobs.length ? state.jobs.map((job) => `
    <button class="queue-item ${state.current?.id === job.id ? "active" : ""}" data-id="${job.id}">
      <div class="queue-file">${escapeHtml(job.source_file)}</div>
      <div class="queue-meta"><span class="status ${job.status}">${formatStatus(job.status)}</span><span>${job.validation?.blocking_issue_count ?? 0} 涓樆鏂」</span></div>
    </button>`).join("") : '<div class="empty-state" style="height:180px"><span>鏆傛棤浠诲姟</span></div>';
  queue.querySelectorAll("[data-id]").forEach((button) => button.addEventListener("click", () => openJob(button.dataset.id)));
  renderAcceptanceSummary();
  if (selectId) await openJob(selectId);
}
function renderAcceptanceSummary() {
  const report = state.acceptance;
  const container = $("#acceptanceSummary");
  if (!report) { container.innerHTML = ""; return; }
  const finalCheck = (report.checks || []).find((entry) => entry.name === "real_template_still_required_for_final_acceptance");
  const fixedExport = (report.checks || []).find((entry) => entry.name === "fixed_template_export");
  const anomalyGuard = (report.checks || []).find((entry) => entry.name === "anomaly_pdf_triggers_alert_and_blocks_approval");
  const missingInputs = (report.required_inputs || []).filter((entry) => !entry.present);
  const nextActions = report.next_actions || [];
  const immutablePolicy = report.immutable_excel_policy || null;
  const modeLabel = report.template_mode === "auto_real_template" ? "正式模板验收" : report.template_mode === "real_template" ? "指定模板验收" : "样例验收";
  container.innerHTML = `
    <div class="acceptance-heading">
      <strong>验收状态</strong>
      <span class="acceptance-mode ${finalCheck?.passed ? "ready" : "pending"}">${escapeHtml(modeLabel)}</span>
    </div>
    ${immutablePolicy ? `<div class="acceptance-policy"><strong>${escapeHtml(immutablePolicy.title)}</strong><span>${escapeHtml(immutablePolicy.detail || "")}</span></div>` : ""}
    <div class="acceptance-note">${escapeHtml(finalCheck?.evidence?.note || "尚未生成验收摘要。")}</div>
    <div class="acceptance-metrics">
      <div class="acceptance-metric">
        <span>固定模板导出</span>
        <strong class="${fixedExport?.passed ? "ok" : "warn"}">${fixedExport?.passed ? "通过" : "未通过"}</strong>
      </div>
      <div class="acceptance-metric">
        <span>异常阻断告警</span>
        <strong class="${anomalyGuard?.passed ? "ok" : "warn"}">${anomalyGuard?.passed ? "通过" : "未通过"}</strong>
      </div>
      <div class="acceptance-metric">
        <span>最终交付条件</span>
        <strong class="${finalCheck?.passed ? "ok" : "warn"}">${finalCheck?.passed ? "已具备" : "仍缺真实模板"}</strong>
      </div>
    </div>
    ${missingInputs.length ? `<div class="acceptance-missing"><div class="acceptance-missing-title">待补充输入</div>${missingInputs.map((entry) => `
      <div class="acceptance-missing-item">
        <strong>${escapeHtml(entry.label)}</strong>
        <span>${escapeHtml(entry.detail)}</span>
        <code>${escapeHtml(entry.expected_location || "")}</code>
      </div>`).join("")}</div>` : ""}
    ${nextActions.length ? `<div class="acceptance-actions"><div class="acceptance-missing-title">下一步</div>${nextActions.map((entry) => `
      <div class="acceptance-action-item">
        <strong>${escapeHtml(entry.title)}</strong>
        <span>${escapeHtml(entry.detail || "")}</span>
      </div>`).join("")}</div>` : ""}`;
}
async function openJob(id) {
  state.current = await api(`/api/jobs/${id}`);
  state.original = structuredClone(state.current);
  state.itemRows = state.current.quote.items.map((item, index) => ({ original_index: index, item: structuredClone(item) }));
  renderJob();
  await loadQueue();
}
function inputFor(path, candidate, type = "text", label = null) {
  const conf = Number(candidate?.confidence || 0);
  const source = candidate?.source || {};
  return `<div class="field-card">
    <label>${escapeHtml(label || headerLabels[path.split(".").pop()] || totalLabels[path.split(".").pop()] || path)}
      <input data-path="${path}" type="${type}" ${type === "number" ? 'step="any"' : ""} value="${escapeHtml(candidate?.value ?? "")}">
    </label>
    <div class="confidence ${conf < .8 ? "low" : ""}"><span>置信度 ${(conf * 100).toFixed(0)}%</span><span>${source.page ? `第${source.page}页` : "无来源"}</span></div>
  </div>`;
}
function renderJob() {
  const job = state.current;
  $("#emptyState").hidden = true; $("#jobView").hidden = false;
  $("#jobTitle").textContent = job.source_file;
  $("#jobMeta").textContent = `任务 ${job.id} 路 ${formatStatus(job.status)} 路 ${job.quote.document.page_count} 页`;
  const sourceUrl = `/api/jobs/${job.id}/source`;
  $("#sourcePreview").src = sourceUrl;
  $("#sourceMeta").textContent = `${job.source?.size_bytes ?? 0} bytes 路 SHA-256 ${(job.source?.sha256 || "").slice(0, 12)}...`;
  $("#openSourceButton").dataset.sourceUrl = sourceUrl;
  $("#headerFields").innerHTML = Object.entries(job.quote.headers).map(([key, candidate]) => inputFor(`headers.${key}`, candidate)).join("");
  $("#totalFields").innerHTML = Object.entries(job.quote.totals).map(([key, candidate]) => inputFor(`totals.${key}`, candidate, "number", totalLabels[key])).join("");
  renderItemsTable();
  renderSourceVerification();
  renderReviewHistory();
  renderAlertHistory();
  renderIssues();
  const banner = $("#alertBanner");
  banner.hidden = !job.alert;
  if (job.alert) {
    const delivery = job.alert.delivery || {};
    const result = delivery.channel === "webhook" ? (delivery.success ? "Webhook已送达" : "Webhook待重试") : "已本地留痕";
    banner.textContent = `已记录 ${job.alerts?.length || 1} 次告警；最近事件：${job.alert.payload.event}。${result}。`;
  }
  $("#downloadButton").disabled = job.status !== "approved" || !state.templateReady;
  $("#downloadButton").title = state.templateReady
    ? "鎸夊師濮婨xcel妯℃澘瀵煎嚭锛屽彧鍐欏叆鍥哄畾鍗曞厓鏍煎€硷紝涓嶄慨鏀笶xcel鏍煎紡"
    : state.templateReason;
  $("#approveButton").disabled = job.status === "approved";
  $("#reviewer").value = job.review?.reviewer || "人工审核员";
  $("#reviewNote").value = job.review?.note || "";
}
function renderItemsTable() {
  $("#itemCount").textContent = `${state.itemRows.length} 条明细`;
  $("#itemsTable").innerHTML = `<thead><tr><th>序号</th>${itemColumns.map(([, label]) => `<th>${label}</th>`).join("")}<th>置信度</th><th>操作</th></tr></thead>
    <tbody>${state.itemRows.map((row, index) => `<tr data-row-index="${index}" data-original-index="${row.original_index ?? ""}"><td>${index + 1}</td>${itemColumns.map(([key]) => {
      const item = row.item;
      const candidate = item[key] || {}; const numeric = ["quantity","unit_price","amount"].includes(key);
      return `<td><input data-item-key="${key}" type="${numeric ? "number" : "text"}" step="any" value="${escapeHtml(candidate.value ?? "")}" title="来源：${candidate.source?.page ? `第${candidate.source.page}页` : candidate.source?.type === "manual_review" ? "人工录入" : "未知"}；置信度：${Math.round((candidate.confidence || 0) * 100)}%"></td>`;
    }).join("")}<td>${Math.round(Math.min(...itemColumns.map(([key]) => Number(row.item[key]?.confidence || 0))) * 100)}%</td><td><button class="remove-row" data-remove-row="${index}" title="删除此明细" aria-label="删除第${index + 1}行">×</button></td></tr>`).join("")}</tbody>`;
  document.querySelectorAll("[data-remove-row]").forEach((button) => button.addEventListener("click", () => {
    state.itemRows.splice(Number(button.dataset.removeRow), 1);
    renderItemsTable();
  }));
}
function renderSourceVerification() {
  const extractionIssues = state.current.quote.extraction_issues || [];
  const control = $("#sourceVerification");
  control.hidden = extractionIssues.length === 0;
  $("#sourceVerifiedCheckbox").checked = Boolean(state.current.quote.human_source_verification?.verified);
}
function renderReviewHistory() {
  const history = state.current.review_history || [];
  const container = $("#reviewHistory");
  container.innerHTML = history.length ? `<div class="review-history-title">审核记录</div>${history.slice().reverse().map((entry) => `
    <div class="review-entry"><span>${escapeHtml(entry.reviewed_at)}</span><strong>${escapeHtml(entry.reviewer)} 路 ${escapeHtml(entry.outcome)}</strong><span>${escapeHtml(entry.note || `${entry.changed_paths?.length || 0} 个字段变更`)}</span></div>`).join("")}` : "";
}
function renderAlertHistory() {
  const alerts = state.current.alerts || [];
  const container = $("#alertHistory");
  if (!alerts.length) { container.innerHTML = ""; return; }
  const hasFailure = alerts.some((entry) => entry.delivery?.channel === "webhook" && !entry.delivery?.success);
  container.innerHTML = `<div class="alert-history-heading"><div class="review-history-title">告警投递记录</div>${hasFailure ? '<button id="retryAlertsButton" class="secondary-button small-button">立即重试失败告警</button>' : ""}</div>${alerts.slice().reverse().map((entry) => {
    const delivery = entry.delivery || {};
    const delivered = delivery.channel === "webhook" ? Boolean(delivery.success) : true;
    const detail = delivery.channel === "webhook"
      ? `${delivered ? "已送达" : "投递失败"} 路 ${delivery.attempts || 0} 次尝试${delivery.status ? ` 路 HTTP ${delivery.status}` : ""}${delivery.next_retry_at ? ` 路 下次 ${delivery.next_retry_at}` : ""}`
      : "本地告警已保存";
    return `<div class="alert-entry ${delivered ? "delivered" : "failed"}"><span>${escapeHtml(entry.payload?.created_at || "")}</span><strong>${escapeHtml(entry.payload?.event || "alert")}</strong><span>${escapeHtml(detail)}</span></div>`;
  }).join("")}`;
  $("#retryAlertsButton")?.addEventListener("click", retryFailedAlerts);
}
async function retryFailedAlerts() {
  const button = $("#retryAlertsButton");
  if (button) button.disabled = true;
  try {
    const job = await api(`/api/jobs/${state.current.id}/alerts/retry`, { method: "POST" });
    state.current = job; state.original = structuredClone(job); renderJob();
    const failed = (job.alerts || []).filter((entry) => entry.delivery?.channel === "webhook" && !entry.delivery?.success).length;
    showToast(failed ? `重试完成，仍有 ${failed} 条告警待送达。` : "告警已全部送达。");
  } catch (error) { showToast(error.message); }
}
function renderIssues() {
  const validation = state.current.validation;
  $("#issueSummary").textContent = `${validation.blocking_issue_count} 个阻断项 路 ${validation.warning_count} 个提醒`;
  $("#issues").innerHTML = validation.issues.length ? validation.issues.map((entry) => `
    <div class="issue ${entry.severity}"><span class="issue-level">${entry.severity}</span><span class="issue-path">${escapeHtml(entry.path)}</span><span>${escapeHtml(entry.message)}</span></div>`).join("") : '<div class="no-issues">校验通过，可以提交人工审核。</div>';
}
function collectCorrections() {
  const corrections = {};
  document.querySelectorAll("[data-path]").forEach((input) => {
    const parts = input.dataset.path.split(".");
    let original = state.original.quote;
    for (const part of parts) original = Array.isArray(original) ? original[Number(part)] : original?.[part];
    let value = input.value.trim();
    if (input.type === "number") value = value === "" ? null : Number(value);
    if ((original?.value ?? "") !== value) corrections[input.dataset.path] = value;
  });
  return corrections;
}
function collectItemRows() {
  return Array.from(document.querySelectorAll("#itemsTable tbody tr")).map((row) => {
    const values = {};
    row.querySelectorAll("[data-item-key]").forEach((input) => {
      let value = input.value.trim();
      if (input.type === "number") value = value === "" ? null : Number(value);
      values[input.dataset.itemKey] = value;
    });
    const original = row.dataset.originalIndex;
    return { original_index: original === "" ? null : Number(original), values };
  });
}
async function submitReview(action) {
  try {
    const job = await api(`/api/jobs/${state.current.id}/review`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        action,
        expected_revision: state.original.revision ?? 0,
        corrections: collectCorrections(),
        item_rows: collectItemRows(),
        human_verified_source: $("#sourceVerifiedCheckbox").checked,
        reviewer: $("#reviewer").value,
        note: $("#reviewNote").value
      })
    });
    state.current = job; state.original = structuredClone(job); renderJob(); await loadQueue();
    showToast(action === "approve"
      ? (state.templateReady
        ? "报价单已批准，可按原模板导出；系统只写入固定单元格值，不修改Excel格式。"
        : `报价单已批准；${state.templateReason}`)
      : "修正已保存并重新校验。");
  } catch (error) {
    showToast(error.message);
    if (state.current?.id) await openJob(state.current.id);
  }
}

$("#pdfFile").addEventListener("change", async (event) => {
  const file = event.target.files[0]; if (!file) return;
  const form = new FormData(); form.append("file", file);
  try { const job = await api("/api/jobs", { method: "POST", body: form }); await loadQueue(job.id); showToast("PDF已解析并完成异常校验。"); }
  catch (error) { showToast(error.message); }
  event.target.value = "";
});
$("#templateFile").addEventListener("change", async (event) => {
  const file = event.target.files[0]; if (!file) return;
  const form = new FormData(); form.append("file", file);
  try {
    const result = await api("/api/template", { method: "POST", body: form });
    await loadQueue();
    showToast(result.message);
    await openMappingModal();
  } catch (error) { showToast(error.message); }
  event.target.value = "";
});
function mappingInputs(container, values, kind) {
  container.innerHTML = Object.entries(values || {}).map(([path, value]) => `
    <label>${escapeHtml(mappingLabels[path] || path)}
      <input data-mapping-kind="${kind}" data-mapping-path="${escapeHtml(path)}" value="${escapeHtml(value || "")}" autocomplete="off">
    </label>`).join("");
}
async function openMappingModal() {
  state.templateSetup = await api("/api/template/setup");
  if (!state.templateSetup.available) return showToast(state.templateSetup.reason);
  const draft = structuredClone(state.templateSetup.draft);
  state.mappingDraft = draft;
  $("#mappingMeta").textContent = `${state.templateSetup.template_file} 路 SHA-256 ${state.templateSetup.template_sha256.slice(0, 12)}...`;
  const sheet = $("#mappingSheet");
  sheet.innerHTML = state.templateSetup.sheets.map((entry) => `<option value="${escapeHtml(entry.name)}">${escapeHtml(entry.name)} 路 ${escapeHtml(entry.dimension || "未定义区域")} 路 ${entry.formula_count} 个公式</option>`).join("");
  sheet.value = draft.sheet_name;
  $("#mappingStartRow").value = draft.items.start_row || "";
  $("#mappingMaxRows").value = draft.items.max_rows || "";
  mappingInputs($("#mappingHeaders"), draft.header_cells, "header");
  mappingInputs($("#mappingItems"), draft.items.columns, "item");
  mappingInputs($("#mappingTotals"), draft.total_cells, "total");
  $("#mappingReviewer").value = "";
  $("#mappingConfirmation").checked = false;
  $("#mappingModal").hidden = false;
}
function closeMappingModal() { $("#mappingModal").hidden = true; }
function collectMapping() {
  const draft = structuredClone(state.mappingDraft);
  draft.sheet_name = $("#mappingSheet").value;
  draft.items.start_row = Number($("#mappingStartRow").value);
  draft.items.max_rows = Number($("#mappingMaxRows").value);
  document.querySelectorAll("[data-mapping-path]").forEach((input) => {
    const group = input.dataset.mappingKind === "header" ? draft.header_cells : input.dataset.mappingKind === "total" ? draft.total_cells : draft.items.columns;
    group[input.dataset.mappingPath] = input.value.trim().toUpperCase();
  });
  return draft;
}
async function activateMapping() {
  try {
    const result = await api("/api/template/activate", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        reviewer: $("#mappingReviewer").value,
        confirm_format_immutable: $("#mappingConfirmation").checked,
        template_sha256: state.templateSetup.template_sha256,
        mapping: collectMapping()
      })
    });
    closeMappingModal();
    await loadQueue();
    showToast(result.message);
  } catch (error) { showToast(error.message); }
}
async function exportAcceptanceReport() {
  try {
    const report = await api("/api/acceptance");
    const stamp = (report.generated_at || new Date().toISOString()).replace(/[:]/g, "-");
    downloadJson(`quote-acceptance-${stamp}.json`, report);
    const finalCheck = (report.checks || []).find((entry) => entry.name === "real_template_still_required_for_final_acceptance");
    showToast(finalCheck?.passed ? "验收报告已导出，真实模板验收已具备执行条件。" : "验收报告已导出；当前仍缺真实模板或正式映射。");
  } catch (error) { showToast(error.message); }
}
$("#refreshButton").addEventListener("click", () => loadQueue());
$("#openSourceButton").addEventListener("click", () => {
  const url = $("#openSourceButton").dataset.sourceUrl;
  if (url) window.open(url, "_blank", "noopener,noreferrer");
});
$("#acceptanceButton").addEventListener("click", exportAcceptanceReport);
$("#mappingButton").addEventListener("click", openMappingModal);
$("#closeMappingButton").addEventListener("click", closeMappingModal);
$("#cancelMappingButton").addEventListener("click", closeMappingModal);
$("#activateMappingButton").addEventListener("click", activateMapping);
$("#addItemButton").addEventListener("click", () => {
  const item = Object.fromEntries(itemColumns.map(([key]) => [key, { value: null, confidence: 0, source: { type: "manual_review" } }]));
  state.itemRows.push({ original_index: null, item });
  renderItemsTable();
});
$("#saveButton").addEventListener("click", () => submitReview("save"));
$("#approveButton").addEventListener("click", () => submitReview("approve"));
$("#downloadButton").addEventListener("click", () => {
  if (!state.templateReady) return showToast(state.templateReason);
  window.location.href = `/api/jobs/${state.current.id}/excel`;
});
loadQueue().catch((error) => showToast(error.message));

