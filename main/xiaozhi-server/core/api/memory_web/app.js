"use strict";
const $ = (id) => document.getElementById(id);
const state = { mode: "memories", offset: 0, limit: 30, total: 0, editing: null, deleting: null, request: 0 };
const categories = { profile: "个人信息", preference: "偏好", relationship: "人物关系", event: "经历", plan: "计划与约定" };
const origins = { original_seed: "初始原创日常", generated: "对话创作", manual: "手动补充" };
const modes = ["memories", "stories", "messages"];
const date = (value) => value ? new Date(value.length === 10 ? value + "T00:00:00" : value).toLocaleString("zh-CN") : "时间未注明";
function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
async function api(path, options = {}) {
  const headers = { ...options.headers };
  if ($("token").value) headers.Authorization = "Bearer " + $("token").value;
  if (options.body) headers["Content-Type"] = "application/json";
  const response = await fetch("/memory/api/" + path, { ...options, headers, credentials: "same-origin", cache: "no-store" });
  const result = await response.json();
  if (!response.ok || !result.success) throw new Error(result.message || "操作失败");
  return result.data;
}
function pathFor(item) { return item.mode + (item.id ? "/" + encodeURIComponent(item.id) : "") + "?device_id=" + encodeURIComponent(item.device); }
function selection(item) { return { ...item, mode: state.mode, device: $("device").value }; }
async function loadDevices() {
  try {
    const selected = $("device").value;
    const devices = await api("devices");
    $("device").replaceChildren(...devices.map((device) => {
      const option = element("option", device.device_id + " · " + device.memories + " 条记忆");
      option.value = device.device_id;
      return option;
    }));
    $("device").disabled = devices.length === 0;
    if (!devices.length) {
      const option = element("option", "等待设备聊天");
      option.value = "";
      $("device").append(option);
    }
    if (devices.some((device) => device.device_id === selected)) $("device").value = selected;
    state.offset = 0;
    await load();
  } catch (error) { $("notice").textContent = error.message; }
}
function button(label, callback, className) {
  const node = element("button", label, className);
  node.type = "button";
  node.addEventListener("click", callback);
  return node;
}
async function load() {
  const requestId = ++state.request;
  $("notice").textContent = "";
  $("items").replaceChildren();
  $("add-story").disabled = !$("device").value;
  if (!$("device").value) {
    $("empty").hidden = false;
    $("empty").textContent = "管理服务已连接，但还没有设备的聊天记录。请先连接或唤醒小智并说一句话，再点击“刷新”。";
    $("count").textContent = "";
    $("page").textContent = "1";
    $("previous").disabled = true;
    $("next").disabled = true;
    return;
  }
  $("notice").textContent = "正在读取…";
  try {
    const query = new URLSearchParams({ device_id: $("device").value, q: $("query").value, limit: state.limit, offset: state.offset });
    const result = await api(state.mode + "?" + query);
    if (requestId !== state.request) return;
    state.total = result.total;
    $("notice").textContent = "";
    $("count").textContent = "共 " + result.total + " 条";
    $("empty").hidden = result.items.length !== 0;
    $("empty").textContent = $("query").value ? "没有找到匹配的内容，请换个关键词试试。"
      : state.mode === "stories" ? "还没有角色故事。可以添加故事，也可以请小智讲一个比奇堡的新日常。"
      : state.mode === "messages" ? "这台设备还没有聊天文字，请和小智说一句话后刷新。"
      : "这台设备还没有长期记忆。记忆会在聊天后自动提取，稍后刷新即可查看。";
    $("page").textContent = Math.floor(state.offset / state.limit) + 1;
    $("previous").disabled = state.offset === 0;
    $("next").disabled = state.offset + state.limit >= state.total;
    for (const item of result.items) {
      const card = element("article", undefined, state.mode === "messages" ? "archive" : "");
      const meta = element("div", undefined, "meta");
      if (state.mode !== "messages") {
        const chosen = selection(item);
        if (state.mode === "stories") {
          meta.append(element("span", origins[item.origin] || "原创日常", "badge"), element("span", "角色虚构", "badge"));
          card.append(element("h3", item.title));
        } else {
          meta.append(element("span", categories[item.category] || item.category, "badge"), element("span", item.status === "historical" ? "过去的情况" : "当前情况", "badge"));
          if (item.topic) meta.append(element("span", item.topic));
        }
        card.append(meta, element("p", item.content, "content"), element("div", "记录于 " + date(item.created_at), "meta"));
        if (item.event_time) card.append(element("div", "发生于 " + date(item.event_time), "meta"));
        if (item.follow_up_at) card.append(element("div", "适合后续关心 · " + date(item.follow_up_at), "meta"));
        const actions = element("div", undefined, "actions");
        actions.append(button("查看来源", () => showDetail(chosen)), button("编辑", () => edit(chosen)), button("忘掉", () => remove(chosen), "danger"));
        card.append(actions);
      } else {
        meta.append(element("span", item.role === "user" ? "你" : "小智", "badge"), element("span", date(item.created_at)));
        card.append(meta, element("p", item.content, "content"), element("div", "会话 " + item.session_id, "meta"));
        if (item.excluded) card.append(element("div", "仅保留为档案，不参与自动回忆和提取", "meta"));
      }
      $("items").append(card);
    }
  } catch (error) { if (requestId === state.request) $("notice").textContent = error.message; }
}
function localDateTime(value) {
  if (!value) return "";
  const time = new Date(value);
  return new Date(time.getTime() - time.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
}
function edit(item) {
  state.editing = item;
  const story = item.mode === "stories";
  $("edit-heading").textContent = story ? (item.id ? "编辑角色故事" : "添加角色故事") : "编辑记忆";
  $("edit-content-label").textContent = story ? "故事内容" : "记忆内容";
  $("save-edit").textContent = story && !item.id ? "保存故事" : "保存修改";
  $("edit-note").textContent = story ? "保存为本机原创日常，保持海绵宝宝的人物与比奇堡设定一致；不会当作你的现实经历。" : "这里用于纠正记错的信息。生活中的真实变化会保留为不同的历史记忆。";
  $("story-title-field").hidden = !story;
  $("memory-fields").hidden = story;
  $("edit-title").required = story;
  $("edit-title").value = item.title || "";
  $("edit-content").maxLength = story ? 100000 : 4000;
  $("edit-content").value = item.content || "";
  $("edit-category").value = item.category || "event";
  $("edit-topic").value = item.topic || "";
  $("edit-status").value = item.status || "active";
  $("edit-time").value = item.event_time || "";
  $("edit-follow-up").value = localDateTime(item.follow_up_at);
  $("edit-error").textContent = "";
  $("editor").showModal();
}
$("edit-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const submit = event.submitter;
  if (submit) submit.disabled = true;
  try {
    const changes = state.editing.mode === "stories" ? { title: $("edit-title").value.trim(), content: $("edit-content").value.trim() } : {
      content: $("edit-content").value.trim(), category: $("edit-category").value,
      topic: $("edit-topic").value, status: $("edit-status").value,
      event_time: $("edit-time").value || null,
      follow_up_at: $("edit-follow-up").value ? new Date($("edit-follow-up").value).toISOString() : null
    };
    await api(pathFor(state.editing), { method: state.editing.id ? "PATCH" : "POST", body: JSON.stringify(changes) });
    $("editor").close(); await load();
  } catch (error) { $("edit-error").textContent = error.message; }
  finally { if (submit) submit.disabled = false; }
});
async function showDetail(item) {
  $("detail-content").replaceChildren(element("p", "正在读取…")); $("details").showModal();
  try {
    const detail = await api(pathFor(item));
    $("detail-content").replaceChildren(element("p", detail.content, "content"), element("h3", "原始来源"));
    if (!detail.sources.length) $("detail-content").append(element("p", item.mode === "stories" ? "这是本机初始创作或手动补充的故事，不是动画官方剧情。" : "这条记忆来自旧版摘要，没有可追溯的原文。"));
    for (const source of detail.sources) $("detail-content").append(element("div", date(source.created_at) + "\n" + source.content, "source"));
    $("detail-content").append(element("h3", "纠错记录"));
    if (!detail.revisions.length) $("detail-content").append(element("p", "尚未修改。"));
    for (const revision of detail.revisions) $("detail-content").append(element("div", date(revision.changed_at) + " · 纠正前的内容（已作废）\n" + revision.content, "source"));
  } catch (error) { $("detail-content").replaceChildren(element("p", error.message)); }
}
function remove(item) {
  state.deleting = item;
  const story = item.mode === "stories";
  $("delete-heading").textContent = story ? "忘掉这个角色故事？" : "忘掉这条记忆？";
  $("confirm-delete").textContent = story ? "忘掉这个故事" : "忘掉这条记忆";
  $("delete-note").textContent = story ? "删除后不再用于角色回忆；初始故事也不会自动重新添加。原始聊天文字仍保留在本地档案。" : "删除后不再用于回忆，也不会从它的旧原文重新提取。原始聊天文字仍保留在本地档案。";
  $("delete-content").textContent = item.content; $("delete-error").textContent = ""; $("delete-dialog").showModal();
}
$("confirm-delete").addEventListener("click", async () => {
  $("confirm-delete").disabled = true;
  try { await api(pathFor(state.deleting), { method: "DELETE" }); $("delete-dialog").close(); await loadDevices(); }
  catch (error) { $("delete-error").textContent = error.message; }
  finally { $("confirm-delete").disabled = false; }
});
$("cancel-edit").addEventListener("click", () => $("editor").close());
$("close-details").addEventListener("click", () => $("details").close());
$("cancel-delete").addEventListener("click", () => $("delete-dialog").close());
$("search-form").addEventListener("submit", (event) => { event.preventDefault(); state.offset = 0; load(); });
$("device").addEventListener("change", () => { state.offset = 0; load(); });
$("refresh").addEventListener("click", loadDevices);
$("connect").addEventListener("click", loadDevices);
$("add-story").addEventListener("click", () => edit(selection({})));
for (const mode of modes) $(mode + "-tab").addEventListener("click", () => {
  state.mode = mode; state.offset = 0;
  for (const tab of modes) $(tab + "-tab").setAttribute("aria-pressed", String(tab === mode));
  $("add-story").hidden = mode !== "stories";
  $("story-note").hidden = mode !== "stories";
  load();
});
$("previous").addEventListener("click", () => { state.offset = Math.max(0, state.offset - state.limit); load(); });
$("next").addEventListener("click", () => { state.offset += state.limit; load(); });
loadDevices();
