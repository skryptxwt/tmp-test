"use strict";
const el = id => document.getElementById(id);
const value = id => el(id).value.trim();
const values = id => value(id).split(/[,，\n]/).map(x => x.trim()).filter(Boolean);
let batchMode = false;
let method = "assets";
let lastPage = null;
let requestNumber = 0;

function press(id, active) {
  el(id).setAttribute("aria-pressed", String(active));
}
function showFields() {
  const wholeTenant = el("kind").value === "TENANT";
  el("fields").hidden = batchMode;
  el("batch-field").hidden = !batchMode;
  el("tenant").required = !batchMode;
  el("asset").required = !batchMode && !wholeTenant;
  el("asset").disabled = batchMode || wholeTenant;
  el("spread").disabled = batchMode || !wholeTenant;
  el("rows").required = batchMode;
  for (const id of ["asset-field", "ip-field", "extra"]) el(id).hidden = wholeTenant;
  el("spread-field").hidden = !wholeTenant;
  press("single", !batchMode);
  press("batch", batchMode);
}
function setFeedback(text, error = false) {
  el("feedback").textContent = text;
  el("feedback").classList.toggle("error", error);
}
for (const id of ["single", "batch"]) {
  el(id).onclick = () => {
    batchMode = id === "batch";
    showFields();
    setFeedback("");
  };
}
el("kind").onchange = showFields;
el("example").onclick = () => {
  if (batchMode) {
    el("rows").value = JSON.stringify([
      {tenant_id: "tenant-a", kind: "UPDATE", asset_id: 1001, attrs: ["assetIp"], ips: ["10.0.0.11"]},
      {tenant_id: "tenant-b", kind: "TENANT", attrs: ["managed_level"], spread_sec: 120}
    ], null, 2);
  } else {
    el("form").reset();
    el("tenant").value = "tenant-a";
    el("asset").value = "1001";
    el("attrs").value = "managed_level";
    el("ips").value = "10.0.0.11, 10.0.0.21";
    showFields();
  }
  setFeedback("示例已填入，提交后生效。");
};
function formChange() {
  const event = {tenant_id: value("tenant"), kind: value("kind"), attrs: values("attrs")};
  if (event.kind === "TENANT") {
    event.spread_sec = Number(value("spread"));
  } else {
    event.asset_id = Number(value("asset"));
    event.ips = values("ips");
    event.macs = values("macs");
    if (value("origin-devs")) event.origin_devs = JSON.parse(value("origin-devs"));
    event.endpoint_keys = value("endpoint_keys").split(/\r?\n/).filter(Boolean);
    for (const id of ["dev_id", "dev_type", "agent_id"]) event[id] = value(id);
  }
  return event;
}
async function api(path, options) {
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error([data.status, data.error || "请求失败"].filter(Boolean).join(" · "));
  return data;
}
async function preview() {
  const serial = ++requestNumber;
  const request = {cursor: value("cursor"), until_cursor: value("until"), limit: Number(value("limit"))};
  const params = new URLSearchParams({method, ...request});
  if (method === "assets") {
    request.tenant_ids = values("tenant-filter");
    for (const tenant of request.tenant_ids) params.append("tenant_id", tenant);
  }
  el("request-body").textContent = JSON.stringify(request, null, 2);
  const started = performance.now();
  lastPage = null;
  el("next").hidden = true;
  el("page-info").textContent = "查询中…";
  try {
    const {target, response} = await api("/api/preview?" + params);
    if (serial !== requestNumber) return;
    lastPage = response;
    el("target").textContent = "gRPC · " + target;
    el("output").textContent = JSON.stringify(response, null, 2);
    const count = (response.changes || response.tenants).length;
    const summary = response.gap ? "日志存在缺口，请调整起点游标。" :
      `本页 ${count} 项 · 下一游标 ${response.next_cursor} · 最新游标 ${response.now_cursor}`;
    el("page-info").textContent = `OK · ${Math.round(performance.now() - started)} ms · ${summary}`;
    el("next").hidden = !response.has_more;
  } catch (err) {
    if (serial !== requestNumber) return;
    el("output").textContent = err.message;
    el("page-info").textContent = "查询失败";
    el("target").textContent = "gRPC 查询失败";
  }
}
for (const id of ["assets", "tenants"]) {
  el(id).onclick = () => {
    method = id;
    el("filter-field").hidden = id === "tenants";
    el("method").textContent = "/IdtProxy/" + (id === "tenants" ? "ListChangedTenants" : "ListAssetChanges");
    press("assets", id === "assets");
    press("tenants", id === "tenants");
    preview();
  };
}
el("refresh").onclick = () => {
  el("cursor").value = "";
  el("until").value = "";
  preview();
};
el("query-form").onsubmit = event => {
  event.preventDefault();
  preview();
};
el("next").onclick = () => {
  if (!lastPage) return;
  el("cursor").value = lastPage.next_cursor;
  if (!value("until")) el("until").value = lastPage.now_cursor;
  preview();
};
el("form").onsubmit = async event => {
  event.preventDefault();
  if (el("submit").disabled) return;
  el("submit").disabled = true;
  setFeedback("正在写入…");
  try {
    const rows = batchMode ? JSON.parse(value("rows")) : [formChange()];
    const result = await api("/api/changes", {method: "POST",
      headers: {"Content-Type": "application/json"}, body: JSON.stringify(rows)});
    el("cursor").value = result.cursor;
    el("until").value = result.now_cursor;
    el("tenant-filter").value = "";
    setFeedback(`已写入 ${result.count} 条，正在查看本次提交的区间 (${result.cursor}, ${result.now_cursor}]。`);
    await preview();
  } catch (err) {
    setFeedback(err.message, true);
  } finally {
    el("submit").disabled = false;
  }
};
const sourceDrafts = {AssetQueryByIp: el("source-response").value, AuthQueryByIp: '{"user_id":"user-01","user_name":"test-user"}'};
let sourceMethod = value("source-method");
el("source-method").onchange = () => {
  sourceDrafts[sourceMethod] = el("source-response").value;
  sourceMethod = value("source-method");
  el("source-response").value = sourceDrafts[sourceMethod];
  el("source-output").hidden = true;
  el("source-feedback").textContent = "";
};
el("source-form").onsubmit = async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const action = event.submitter?.value || "read";
  const key = {method: sourceMethod, tenant_id: value("source-tenant"), ip: value("source-ip")};
  if (form.ariaBusy === "true") return;
  form.ariaBusy = "true";
  const controls = [...form.elements];
  controls.forEach(control => control.disabled = true);
  el("source-output").hidden = true;
  let message = "";
  try {
    if (action !== "read") {
      const payload = {...key, response: JSON.parse(value("source-response"))};
      if (action === "notify") payload.change = JSON.parse(value("source-event"));
      const saved = await api("/api/source", {method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify(payload)});
      message = action === "notify" ? `回源值及通知已保存，游标 ${saved.now_cursor}。` : "回源值已保存。";
      if (action === "notify") {
        el("cursor").value = saved.cursor;
        el("until").value = saved.now_cursor;
        el("tenant-filter").value = "";
        await preview();
      }
    }
    const result = await api("/api/preview?" + new URLSearchParams(key));
    el("source-output").textContent = JSON.stringify(result.response, null, 2);
    el("source-output").hidden = false;
    el("source-feedback").textContent = message + key.method + " · OK";
  } catch (err) {
    el("source-feedback").textContent = message + err.message;
  } finally {
    controls.forEach(control => control.disabled = false);
    form.ariaBusy = "false";
  }
};
showFields();
preview();
