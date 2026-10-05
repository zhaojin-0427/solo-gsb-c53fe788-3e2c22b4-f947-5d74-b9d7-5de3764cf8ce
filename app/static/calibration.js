// 实测响应系数校准视图。
// 与 app.js 共享全局作用域（escapeHtml、showToast、formatErrors、switchView）。
// 校准只读取已保存快照的设定曲线与初温，不修改任何快照或诊断；
// 结果仅可填回当前表单。

const calState = {
  items: [],          // curve-capable snapshot summaries from /api/snapshots
  details: new Map(), // snapshot id -> full snapshot JSON (selection order)
  result: null,       // last calibration response
  runSeq: 0,          // arbitration token for calibration runs
};

const calEls = {};
// Token for snapshot-list / snapshot-detail loads; toggling snapshots quickly
// must not let stale detail responses re-render the entry grids.
let calListSeq = 0;

function onCalibrationReady() {
  calEls.list = document.getElementById("calSnapshotList");
  calEls.entries = document.getElementById("calEntryList");
  calEls.hint = document.getElementById("calSelectionHint");
  calEls.calibrateBtn = document.getElementById("calibrateBtn");
  calEls.applyBtn = document.getElementById("applyCalBtn");
  calEls.result = document.getElementById("calResultPanel");

  calEls.calibrateBtn.addEventListener("click", runCalibration);
  calEls.applyBtn.addEventListener("click", applyCalibration);
}

document.addEventListener("DOMContentLoaded", onCalibrationReady);

// Called by app.js each time the calibration view is entered.
async function refreshCalibration() {
  const seq = ++calListSeq;
  try {
    const response = await fetch("/api/snapshots");
    const data = await response.json();
    if (seq !== calListSeq) return;
    calState.items = (data.items || []).filter(
      (item) => item.status === "optimal" || item.status === "feasible_not_optimal"
    );
    const alive = new Set(calState.items.map((item) => item.id));
    for (const id of Array.from(calState.details.keys())) {
      if (!alive.has(id)) calState.details.delete(id);
    }
    renderCalList();
    renderCalEntries();
  } catch (error) {
    if (seq === calListSeq) {
      calEls.list.innerHTML = '<p class="muted">快照列表加载失败。</p>';
    }
  }
}

function renderCalList() {
  if (!calState.items.length) {
    calEls.list.innerHTML =
      '<p class="muted">暂无“已找到曲线”的快照。请先在“曲线试算”页求解出可行曲线。</p>';
  } else {
    calEls.list.innerHTML = calState.items.map((item) => `
      <label class="cal-snapshot-item">
        <input type="checkbox" data-cal-id="${item.id}" ${calState.details.has(item.id) ? "checked" : ""} />
        <span><strong>#${item.id}</strong> · ${escapeHtml(item.status_text)} · ${item.duration ?? "-"} 分钟 ·
          ${escapeHtml(item.created_at.replace("T", " ").replace("+00:00", " UTC"))}</span>
      </label>
    `).join("");
    calEls.list.querySelectorAll("input[data-cal-id]").forEach((box) => {
      box.addEventListener("change", () => {
        toggleCalSnapshot(Number(box.dataset.calId), box.checked);
      });
    });
  }
  updateCalHint();
}

function updateCalHint() {
  const count = calState.details.size;
  calEls.hint.textContent = count < 2 ? `已选 ${count} 条，至少选择 2 条` : `已选 ${count} 条`;
}

async function toggleCalSnapshot(id, checked) {
  if (!checked) {
    calState.details.delete(id);
    invalidateCalResult();
    renderCalEntries();
    updateCalHint();
    return;
  }
  const seq = calListSeq;
  try {
    const response = await fetch(`/api/snapshots/${id}`);
    if (!response.ok) throw new Error("快照载入失败或不存在");
    const detail = await response.json();
    // The checkbox may have been unchecked or the list refreshed meanwhile.
    const box = calEls.list.querySelector(`input[data-cal-id="${id}"]`);
    if (seq !== calListSeq || !box || !box.checked) return;
    calState.details.set(id, detail);
    invalidateCalResult();
    renderCalEntries();
    updateCalHint();
  } catch (error) {
    showToast(error.message, true);
    const box = calEls.list.querySelector(`input[data-cal-id="${id}"]`);
    if (box) box.checked = false;
  }
}

// A changed snapshot selection invalidates any previous calibration result.
function invalidateCalResult() {
  calState.result = null;
  calState.runSeq += 1;
  calEls.result.innerHTML = "";
  calEls.applyBtn.disabled = true;
}

// Signature of the selection currently rendered in the entry area; re-renders
// with an unchanged selection are skipped so typed readings survive view
// switches and list refreshes.
let calEntriesSignature = null;

function renderCalEntries() {
  const signature = Array.from(calState.details.keys()).sort((a, b) => a - b).join(",");
  if (signature === calEntriesSignature) return;
  calEntriesSignature = signature;
  if (!calState.details.size) {
    calEls.entries.innerHTML = '<p class="muted">尚未选择快照。</p>';
    return;
  }
  calEls.entries.innerHTML = Array.from(calState.details.values()).map((detail) => {
    const input = detail.input;
    const zoneHeaders = input.zones.map((zone, zoneIndex) => `
      <th colspan="2">${escapeHtml(zone.name || `温区 ${zoneIndex + 1}`)}（初温 ${zone.initial_temperature}℃）</th>
    `).join("");
    const seriesHeaders = input.zones.map(() => "<th>序列 1</th><th>序列 2</th>").join("");
    const rows = detail.setpoints.map((setpoint, index) => {
      const minute = index + 1;
      const cells = input.zones.map((_, zoneIndex) => [0, 1].map((seriesIndex) => `
        <td><input type="number" min="0" max="300" step="any" placeholder="—"
               data-snap="${detail.snapshot_id}" data-zone="${zoneIndex}"
               data-series="${seriesIndex}" data-minute="${minute}" /></td>
      `).join("")).join("");
      return `<tr><td>${minute}</td><td>${setpoint}</td>${cells}</tr>`;
    }).join("");
    return `
      <div class="cal-entry-card">
        <div class="cal-entry-title">
          <strong>快照 #${detail.snapshot_id}</strong>
          <span class="pill good">${escapeHtml(detail.status_text)}</span>
          <span class="muted">时长 ${input.duration} 分钟 · 保存的设定曲线：${detail.setpoints.join("、")}</span>
        </div>
        <div class="table-wrap">
          <table class="cal-table">
            <thead>
              <tr><th rowspan="2">分钟</th><th rowspan="2">设定 U(t)</th>${zoneHeaders}</tr>
              <tr>${seriesHeaders}</tr>
            </thead>
            <tbody>${rows}</tbody>
          </table>
        </div>
      </div>`;
  }).join("");
}

// Collect the payload, sending temperatures as the raw input strings so the
// server parses the exact finite decimals the user typed.
function collectCalPayload() {
  if (calState.details.size < 2) {
    throw new Error("请至少选择两条“已找到曲线”的不同快照。");
  }
  const snapshots = [];
  for (const [id, detail] of calState.details) {
    const zones = [0, 1].map((zoneIndex) => {
      const zoneName = detail.input.zones[zoneIndex].name || `温区 ${zoneIndex + 1}`;
      const series = [0, 1].map((seriesIndex) => {
        const inputs = calEls.entries.querySelectorAll(
          `input[data-snap="${id}"][data-zone="${zoneIndex}"][data-series="${seriesIndex}"]`
        );
        const readings = [];
        inputs.forEach((inputEl) => {
          const raw = inputEl.value.trim();
          if (raw === "") return;
          const value = Number(raw);
          if (!Number.isFinite(value) || value < 0 || value > 300) {
            throw new Error(
              `快照 #${id} ${zoneName} 序列 ${seriesIndex + 1} 第 ${inputEl.dataset.minute} 分钟的温度“${raw}”非法：须为 0～300℃ 的有限十进制数。`
            );
          }
          readings.push({ minute: Number(inputEl.dataset.minute), temperature: raw });
        });
        return readings;
      });
      return { series };
    });
    snapshots.push({ snapshot_id: id, zones });
  }
  return { snapshots };
}

async function runCalibration() {
  let payload;
  try {
    payload = collectCalPayload();
  } catch (error) {
    showToast(error.message, true);
    return;
  }
  const seq = ++calState.runSeq;
  calEls.calibrateBtn.disabled = true;
  calEls.calibrateBtn.textContent = "校准中…";
  calEls.applyBtn.disabled = true;
  try {
    const response = await fetch("/api/calibrate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });
    const data = await response.json();
    if (seq !== calState.runSeq) return;
    if (!response.ok) {
      calState.result = null;
      renderCalError(data.detail);
      return;
    }
    calState.result = data;
    renderCalResult(data);
  } catch (error) {
    if (seq === calState.runSeq) showToast(`校准请求失败：${error.message}`, true);
  } finally {
    if (seq === calState.runSeq) {
      calEls.calibrateBtn.disabled = false;
      calEls.calibrateBtn.textContent = "开始校准";
    }
  }
}

function renderCalError(detail) {
  const info = typeof detail === "object" && detail !== null && !Array.isArray(detail)
    ? detail
    : { message: formatErrors(detail) };
  calEls.result.innerHTML = `
    <div class="diagnosis-error">
      <strong>校准失败</strong>
      <div>${escapeHtml(info.message || "请求失败")}</div>
      ${info.code ? `<div class="diagnosis-code">错误类型：${escapeHtml(info.code)}</div>` : ""}
    </div>`;
  calEls.applyBtn.disabled = true;
}

function exactTitle(payload) {
  return `精确值：${payload.fraction} = ${payload.decimal}`;
}

function renderCalResult(data) {
  calEls.result.innerHTML = `
    <div class="cal-result-inner">
      <div class="diagnosis-header">
        <h3>校准结果</h3>
        <div class="diagnosis-meta">
          耗时 ${data.elapsed_ms} ms · 参与快照 ${data.snapshots.map((s) => `#${s.snapshot_id}`).join("、")}
        </div>
      </div>
      ${data.applicable
        ? '<p class="cal-applicable ok">两区系数对均唯一，可点击“填回表单”把校准系数写入当前表单（不会改动已保存的快照与诊断）。</p>'
        : '<p class="cal-applicable bad">存在不可辨识的温区（不同系数对同分并列最优），整次套用已禁用。</p>'}
      ${data.zones.map(renderCalZone).join("")}
    </div>`;
  calEls.applyBtn.disabled = !data.applicable;
  calEls.result.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function renderCalZone(zone) {
  if (!zone.identifiable) {
    const pairs = zone.tied_pairs
      .map((pair) => `<span class="sp">(${pair.coefficients.join(", ")})</span>`)
      .join("");
    const more = zone.tied_pairs_count > zone.tied_pairs.length
      ? `<span class="muted">… 共 ${zone.tied_pairs_count} 对，仅列前 ${zone.tied_pairs.length} 对</span>`
      : "";
    return `
      <div class="cal-zone unidentifiable">
        <h4>${escapeHtml(zone.zone_name)}（温区 ${zone.zone_index}）<span class="badge no_solution">不可辨识</span></h4>
        <p>共 ${zone.tied_pairs_count} 个不同系数对并列最优，总平方误差均为
          <strong title="${exactTitle(zone.min_sse)}">${zone.min_sse.display}</strong>，
          无法确定唯一系数对，整次套用已禁用：</p>
        <div class="witness-setpoints">${pairs}${more}</div>
      </div>`;
  }
  const best = zone.best;
  return `
    <div class="cal-zone">
      <h4>${escapeHtml(zone.zone_name)}（温区 ${zone.zone_index}）唯一最优系数对
        <span class="pill good">α₁ = ${best.coefficients[0]}</span>
        <span class="pill good">α₂ = ${best.coefficients[1]}</span>
        ${best.attribution_tied ? '<span class="pill warn">存在归属并列</span>' : ""}
      </h4>
      <p class="cal-sse">全部读数平方误差之和：
        <strong title="${exactTitle(zone.min_sse)}">${zone.min_sse.display}</strong>
        <span class="muted">（精确有理数比较，未按显示精度舍入）</span>
      </p>
      ${best.assignments.map(renderCalAssignment).join("")}
    </div>`;
}

function renderCalAssignment(assignment) {
  const blocks = assignment.series.map((series) => {
    const rows = series.residuals.map((r) => `
      <tr>
        <td>${r.minute}</td>
        <td title="${exactTitle(r.observed)}">${r.observed.display}</td>
        <td title="${exactTitle(r.predicted)}">${r.predicted.display}</td>
        <td title="${exactTitle(r.residual)}">${r.residual.display}</td>
      </tr>`).join("");
    return `
      <div class="cal-series">
        <div class="cal-series-title">
          序列 ${series.series_index} → α = ${series.coefficient}
          <span class="muted">序列平方误差
            <span title="${exactTitle(series.sse)}">${series.sse.display}</span></span>
        </div>
        <div class="table-wrap">
          <table class="cal-table">
            <thead><tr><th>分钟</th><th>实测温度</th><th>预测温度</th><th>残差（实测−预测）</th></tr></thead>
            <tbody>${rows}</tbody>
          </table>
        </div>
      </div>`;
  }).join("");
  return `
    <div class="cal-assignment">
      <div class="cal-assignment-title">
        快照 #${assignment.snapshot_id} 序列归属：
        序列 1 → α = ${assignment.series[0].coefficient}，序列 2 → α = ${assignment.series[1].coefficient}
        ${assignment.tied ? '<span class="pill warn">归属并列：两种归属总误差相同</span>' : ""}
      </div>
      ${blocks}
    </div>`;
}

// Fill the calibrated coefficients back into the trial form. This only
// touches the current form; saved snapshots and diagnoses are never modified.
function applyCalibration() {
  const result = calState.result;
  if (!result || !result.applicable) return;
  const cards = document.querySelectorAll(".zone-card");
  result.zones.forEach((zone, zoneIndex) => {
    const card = cards[zoneIndex];
    if (!card) return;
    card.querySelector('[data-field="coefficient-0"]').value = zone.best.coefficients[0];
    card.querySelector('[data-field="coefficient-1"]').value = zone.best.coefficients[1];
  });
  switchView("solve");
  showToast("已将校准系数填回表单（未改动任何已保存的快照或诊断）");
}
