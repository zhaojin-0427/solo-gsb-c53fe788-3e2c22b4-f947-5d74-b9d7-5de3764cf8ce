const els = {};
// Snapshots chosen for calibration, keyed by snapshot id in selection order.
const selected = new Map();
// Monotonic token: only the newest calibration run may touch the result panel.
let calibSeq = 0;
let lastResult = null;

function onReady() {
  els.picker = document.getElementById("snapshotPicker");
  els.entryPanels = document.getElementById("entryPanels");
  els.entryHint = document.getElementById("entryHint");
  els.calibrateBtn = document.getElementById("calibrateBtn");
  els.panel = document.getElementById("calibrationPanel");
  els.badge = document.getElementById("calibBadge");
  els.meta = document.getElementById("calibMeta");
  els.message = document.getElementById("calibMessage");
  els.body = document.getElementById("calibBody");
  els.applyBtn = document.getElementById("applyBtn");
  els.toast = document.getElementById("toast");

  els.calibrateBtn.addEventListener("click", runCalibration);
  els.applyBtn.addEventListener("click", applyToForm);
  loadSnapshotPicker();
}

async function loadSnapshotPicker() {
  try {
    const response = await fetch("/api/snapshots");
    const data = await response.json();
    renderPicker(data.items || []);
  } catch (error) {
    els.picker.innerHTML = '<p class="muted">快照列表加载失败。</p>';
  }
}

function hasCurve(item) {
  return item.status === "optimal" || item.status === "feasible_not_optimal";
}

function renderPicker(items) {
  if (!items.length) {
    els.picker.innerHTML = '<p class="muted">暂无快照，请先在试算页求解。</p>';
    return;
  }
  els.picker.innerHTML = items.map((item) => {
    const usable = hasCurve(item);
    const when = escapeHtml(item.created_at.replace("T", " ").replace("+00:00", " UTC"));
    return `
      <label class="snapshot-option ${usable ? "" : "disabled"}">
        <input type="checkbox" data-id="${item.id}" ${usable ? "" : "disabled"} />
        <span>
          <strong>#${item.id}</strong> · ${escapeHtml(item.status_text)}${usable ? "" : "（无曲线）"}<br>
          <span class="muted">${item.duration ?? "-"} 分钟 · ${when}</span>
        </span>
      </label>`;
  }).join("");
  els.picker.querySelectorAll("input[type=checkbox]").forEach((box) => {
    box.addEventListener("change", () => toggleSnapshot(Number(box.dataset.id), box.checked));
  });
}

async function toggleSnapshot(id, checked) {
  if (!checked) {
    selected.delete(id);
    syncEntryPanels();
    return;
  }
  try {
    const response = await fetch(`/api/snapshots/${id}`);
    if (!response.ok) throw new Error("快照载入失败或不存在");
    const detail = await response.json();
    selected.set(id, detail);
    syncEntryPanels();
  } catch (error) {
    const box = els.picker.querySelector(`input[data-id="${id}"]`);
    if (box) box.checked = false;
    showToast(`快照 #${id} 载入失败：${error.message}`, true);
  }
}

function syncEntryPanels() {
  els.entryHint.hidden = selected.size > 0;
  // Drop panels for deselected snapshots, keep the rest untouched so any
  // readings already typed in survive re-selection of other snapshots.
  Array.from(els.entryPanels.children).forEach((child) => {
    if (!selected.has(Number(child.dataset.snapshot))) child.remove();
  });
  const ordered = [...selected.keys()].sort((a, b) => a - b);
  ordered.forEach((id, index) => {
    let panel = els.entryPanels.querySelector(`[data-snapshot="${id}"]`);
    if (!panel) panel = buildEntryPanel(selected.get(id));
    if (els.entryPanels.children[index] !== panel) {
      els.entryPanels.insertBefore(panel, els.entryPanels.children[index] || null);
    }
  });
}

function buildEntryPanel(detail) {
  const id = detail.snapshot_id;
  const duration = detail.input.duration;
  const card = document.createElement("div");
  card.className = "entry-card";
  card.dataset.snapshot = id;
  card.innerHTML = `
    <div class="entry-head">
      <h3>快照 #${id} · 时长 ${duration} 分钟</h3>
      <span class="muted">保存的设定曲线 U(1..${duration}) = [${detail.setpoints.join(", ")}]</span>
    </div>
    <div class="entry-zones">
      ${detail.input.zones.map((zone, zoneIndex) => `
        <div class="entry-zone" data-zone="${zoneIndex}">
          <h4>温区 ${zoneIndex + 1} · ${escapeHtml(zone.name || `温区 ${zoneIndex + 1}`)}（初温 ${zone.initial_temperature}℃）</h4>
          <div class="series-pair">
            ${[0, 1].map((seriesIndex) => `
              <div class="series-editor" data-series="${seriesIndex}">
                <div class="series-head">
                  <span>序列 ${seriesIndex + 1}（未标号）</span>
                  <button type="button" class="small secondary add-reading">添加读数</button>
                </div>
                <div class="reading-rows"></div>
              </div>`).join("")}
          </div>
        </div>`).join("")}
    </div>`;
  card.querySelectorAll(".series-editor").forEach((editor) => {
    editor.querySelector(".add-reading").addEventListener("click", () => addReadingRow(editor, duration));
    addReadingRow(editor, duration);
  });
  return card;
}

function addReadingRow(editor, duration) {
  const row = document.createElement("div");
  row.className = "reading-row";
  row.innerHTML = `
    <input class="r-minute" type="number" min="1" max="${duration}" step="1" placeholder="分钟 1～${duration}" />
    <input class="r-temp" type="text" inputmode="decimal" placeholder="温度 0～300 有限小数" />
    <button type="button" class="small secondary remove-reading" title="删除该读数">×</button>`;
  row.querySelector(".remove-reading").addEventListener("click", () => row.remove());
  editor.querySelector(".reading-rows").appendChild(row);
}

const TEMP_PATTERN = /^[+-]?(\d+(\.\d*)?|\.\d+)$/;

function collectPayload() {
  if (selected.size < 2) {
    showToast("请至少选择两条已找到曲线的不同快照。", true);
    return null;
  }
  const snapshots = [];
  for (const card of els.entryPanels.querySelectorAll(".entry-card")) {
    const id = Number(card.dataset.snapshot);
    const zones = [];
    let problem = null;
    card.querySelectorAll(".entry-zone").forEach((zoneEl, zoneIndex) => {
      const series = [];
      zoneEl.querySelectorAll(".series-editor").forEach((editor, seriesIndex) => {
        const label = `快照 #${id} 温区 ${zoneIndex + 1} 序列 ${seriesIndex + 1}`;
        const readings = [];
        editor.querySelectorAll(".reading-row").forEach((row) => {
          if (problem) return;
          const minuteText = row.querySelector(".r-minute").value.trim();
          const tempText = row.querySelector(".r-temp").value.trim();
          if (minuteText === "" && tempText === "") return;
          if (minuteText === "" || tempText === "") {
            problem = `${label} 存在只填了一半的读数行，请补全或删除。`;
            return;
          }
          if (!/^\d+$/.test(minuteText)) {
            problem = `${label} 的分钟“${minuteText}”不是正整数。`;
            return;
          }
          if (!TEMP_PATTERN.test(tempText)) {
            problem = `${label} 的温度“${tempText}”不是有限十进制数。`;
            return;
          }
          readings.push({ minute: Number(minuteText), temperature: tempText });
        });
        series.push(readings);
      });
      zones.push({ series });
    });
    if (problem) {
      showToast(problem, true);
      return null;
    }
    snapshots.push({ snapshot_id: id, zones });
  }
  return { snapshots };
}

async function runCalibration() {
  const payload = collectPayload();
  if (!payload) return;
  const seq = ++calibSeq;
  els.calibrateBtn.disabled = true;
  els.calibrateBtn.textContent = "校准中…";
  try {
    const response = await fetch("/api/calibrate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });
    const data = await response.json();
    if (seq !== calibSeq) return;
    if (!response.ok) {
      showToast(formatErrors(data.detail), true);
      return;
    }
    renderCalibration(data);
  } catch (error) {
    if (seq === calibSeq) showToast(`校准请求失败：${error.message}`, true);
  } finally {
    if (seq === calibSeq) {
      els.calibrateBtn.disabled = false;
      els.calibrateBtn.textContent = "开始校准";
    }
  }
}

function renderCalibration(data) {
  lastResult = data;
  els.panel.hidden = false;
  els.badge.textContent = data.identifiable ? "两区均可辨识" : "存在不可辨识温区";
  els.badge.className = `badge ${data.identifiable ? "optimal" : "no_solution"}`;
  els.meta.innerHTML = `校准耗时 ${data.elapsed_ms} ms<br>快照 ${data.snapshot_ids.map((id) => `#${id}`).join("、")}`;
  els.message.textContent = data.message;
  els.applyBtn.disabled = !data.identifiable;
  els.applyBtn.title = data.identifiable ? "" : "存在并列最优系数对，整次套用已禁用";
  els.body.innerHTML = data.zones.map(renderZoneResult).join("");
  els.panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function renderZoneResult(zone) {
  if (zone.status === "tied") {
    const shown = zone.tied_pairs || [];
    const pairs = shown.map((pair) =>
      `<span class="tied-pair">{${pair[0]}, ${pair[1]}}</span>`).join("");
    const more = zone.tied_count > shown.length ? `（仅列出前 ${shown.length} 对）` : "";
    return `
      <div class="zone-result">
        <div class="trajectory-title">
          <h3>温区 ${zone.zone_index} · ${escapeHtml(zone.zone_name || "")}</h3>
          <span class="pill bad">不可辨识：${zone.tied_count} 对系数并列最优</span>
          <span class="pill" title="精确值：${zone.sse.fraction} = ${zone.sse.decimal}">最小平方误差和 ${zone.sse.display}</span>
        </div>
        <p class="muted">不同的无序系数对取得完全相同的最小平方误差和，无法唯一确定该区系数，整次套用已禁用。${more}</p>
        <div class="tied-pairs">${pairs}</div>
      </div>`;
  }
  return `
    <div class="zone-result">
      <div class="trajectory-title">
        <h3>温区 ${zone.zone_index} · ${escapeHtml(zone.zone_name || "")}</h3>
        <span class="pill good">唯一最优系数对</span>
        <span class="pill good">α₁ = ${zone.coefficients[0]}</span>
        <span class="pill good">α₂ = ${zone.coefficients[1]}</span>
        <span class="pill" title="精确值：${zone.sse.fraction} = ${zone.sse.decimal}">最小平方误差和 ${zone.sse.display}</span>
      </div>
      ${(zone.assignments || []).map(renderAssignment).join("")}
    </div>`;
}

function renderAssignment(assignment) {
  const mapping = assignment.series.map((s) => `序列 ${s.series} → α=${s.coefficient}`).join("，");
  const tieBadge = assignment.tie
    ? '<span class="pill bad">归属并列：两种归属误差完全相同</span>'
    : "";
  const tables = assignment.series.map((series) => `
    <div class="table-wrap">
      <table>
        <thead>
          <tr><th colspan="4">序列 ${series.series} → α=${series.coefficient}（平方误差和 ${series.sse.display}）</th></tr>
          <tr><th>分钟 t</th><th>实测温度</th><th>预测温度</th><th>残差（实测−预测）</th></tr>
        </thead>
        <tbody>
          ${series.readings.map((r) => `
            <tr>
              <td>${r.minute}</td>
              <td title="精确值：${r.measured.fraction} = ${r.measured.decimal}">${r.measured.display}</td>
              <td title="精确值：${r.predicted.fraction} = ${r.predicted.decimal}">${r.predicted.display}</td>
              <td title="精确值：${r.residual.fraction} = ${r.residual.decimal}">${r.residual.display}</td>
            </tr>`).join("")}
        </tbody>
      </table>
    </div>`).join("");
  return `
    <div class="assignment-card">
      <div class="assignment-head">
        <strong>快照 #${assignment.snapshot_id} · ${escapeHtml(assignment.zone_name || "")}</strong>
        <span>${mapping}</span>
        <span class="pill" title="精确值：${assignment.sse.fraction} = ${assignment.sse.decimal}">该快照误差和 ${assignment.sse.display}</span>
        ${tieBadge}
      </div>
      ${tables}
    </div>`;
}

function applyToForm() {
  if (!lastResult || !lastResult.identifiable) return;
  const coefficients = lastResult.zones.map((zone) => zone.coefficients);
  localStorage.setItem("calibratedCoefficients", JSON.stringify(coefficients));
  location.href = "/";
}

function formatErrors(detail) {
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object" && !Array.isArray(detail)) {
    return detail.message || detail.code || "请求失败";
  }
  if (!Array.isArray(detail)) return "输入参数无效";
  return detail.map((error) => {
    const location = (error.loc || []).slice(1).join(" / ");
    return `${location ? `${location}: ` : ""}${error.msg}`;
  }).join("\n");
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[char]));
}

let toastTimer;
function showToast(message, isError = false) {
  els.toast.textContent = message;
  els.toast.className = `toast${isError ? " error" : ""}`;
  els.toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { els.toast.hidden = true; }, isError ? 6000 : 2500);
}

document.addEventListener("DOMContentLoaded", onReady);
