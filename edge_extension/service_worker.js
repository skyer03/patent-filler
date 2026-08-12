"use strict";

const NATIVE_HOST = "com.company.patent_autofill";
const PROFILE_URL = chrome.runtime.getURL("profiles/dom_profile.json");

function nativeRequest(message) {
  return new Promise((resolve, reject) => {
    let settled = false;
    const port = chrome.runtime.connectNative(NATIVE_HOST);
    port.onMessage.addListener((response) => {
      if (settled) return;
      settled = true;
      port.disconnect();
      if (response && response.ok) resolve(response.payload);
      else reject(new Error(response?.error?.message || "本机组件返回未知错误。"));
    });
    port.onDisconnect.addListener(() => {
      if (settled) return;
      settled = true;
      reject(new Error(chrome.runtime.lastError?.message || "无法连接本机组件。"));
    });
    port.postMessage(message);
  });
}

async function activeTab() {
  const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  if (tabs.length !== 1 || !tabs[0].id) throw new Error("没有唯一的当前 Edge 标签页。");
  if (!/^https?:/i.test(tabs[0].url || "")) throw new Error("当前标签页不是可预填的 HTTP/HTTPS 页面。");
  return tabs[0];
}

async function injectContent(tabId) {
  const results = await chrome.scripting.executeScript({
    target: { tabId, allFrames: true },
    files: ["content.js"],
  });
  const frameIds = [...new Set(results.map((item) => item.frameId))];
  if (!frameIds.length) throw new Error("扩展无法进入当前页面或其 iframe。");
  return frameIds;
}

async function sendFrame(tabId, frameId, message) {
  return chrome.tabs.sendMessage(tabId, message, { frameId });
}

async function loadProfile() {
  const response = await fetch(PROFILE_URL);
  if (!response.ok) throw new Error("扩展内置 DOM Profile 无法读取。");
  return response.json();
}

async function inspectFrames(tabId, frameIds, profile) {
  const inspections = [];
  for (const frameId of frameIds) {
    try {
      const result = await sendFrame(tabId, frameId, { type: "inspect_profile", profile });
      inspections.push({ frameId, ...result });
    } catch (error) {
      inspections.push({ frameId, safe: false, error: String(error?.message || error) });
    }
  }
  return inspections;
}

function chooseExecutionFrame(inspections, task) {
  const requiredIds = task.fields.map((field) => field.field_id);
  const candidates = inspections.filter(
    (item) => item.safe && requiredIds.every((fieldId) => item.field_counts?.[fieldId] === 1),
  );
  if (candidates.length !== 1) {
    const detail = candidates.length === 0 ? "没有 frame 同时满足页面指纹和字段唯一定位" : "多个 frame 同时匹配";
    const diagnostics = inspections.map((item) => ({
      frame_id: item.frameId,
      safe: Boolean(item.safe),
      missing: item.missing || [],
      errors: item.errors || [],
      field_counts: Object.fromEntries(requiredIds.map((fieldId) => [fieldId, item.field_counts?.[fieldId] || 0])),
    }));
    throw new Error(`页面定位不唯一：${detail}。诊断=${JSON.stringify(diagnostics)}`);
  }
  return candidates[0].frameId;
}

async function startFill() {
  const task = await nativeRequest({ type: "get_ready_task" });
  const profile = await loadProfile();
  if (task.profile_version !== profile.version) {
    throw new Error(`任务 Profile ${task.profile_version} 与扩展 Profile ${profile.version} 不一致。`);
  }
  const tab = await activeTab();
  const frameIds = await injectContent(tab.id);
  await chrome.storage.session.set({ currentTask: task, currentTabId: tab.id, currentFrameIds: frameIds });
  const inspections = await inspectFrames(tab.id, frameIds, profile);
  const frameId = chooseExecutionFrame(inspections, task);
  const result = await sendFrame(tab.id, frameId, { type: "fill_task", task, profile });
  if (!result?.completed) {
    try {
      const stopped = await nativeRequest({ type: "finish_task", task_id: task.task_id });
      await chrome.storage.session.set({ lastResult: stopped });
    } finally {
      await chrome.storage.session.remove(["currentTask", "currentTabId", "currentFrameIds"]);
    }
    throw new Error(result?.message || "页面预填未完成。已停止后续动作。");
  }
  const finished = await nativeRequest({ type: "finish_task", task_id: task.task_id });
  await chrome.storage.session.set({ lastResult: finished });
  await chrome.storage.session.remove(["currentTask", "currentTabId", "currentFrameIds"]);
  return finished;
}

async function stopFill() {
  const state = await chrome.storage.session.get(["currentTask", "currentTabId", "currentFrameIds"]);
  const frameIds = Array.isArray(state.currentFrameIds) ? state.currentFrameIds : [];
  if (state.currentTabId) {
    await Promise.allSettled(frameIds.map((frameId) => sendFrame(state.currentTabId, frameId, { type: "stop" })));
  }
  if (state.currentTask?.task_id) {
    try {
      return await nativeRequest({
        type: "cancel_task",
        task_id: state.currentTask.task_id,
        reason_code: "user_stop",
      });
    } finally {
      await chrome.storage.session.remove(["currentTask", "currentTabId", "currentFrameIds"]);
    }
  }
  return { status: "empty" };
}

async function calibration() {
  const tab = await activeTab();
  const frameIds = await injectContent(tab.id);
  const frames = [];
  for (const frameId of frameIds) {
    try {
      const result = await sendFrame(tab.id, frameId, { type: "calibrate" });
      frames.push({ frame_id: frameId, ...result });
    } catch (error) {
      frames.push({ frame_id: frameId, accessible: false, error: String(error?.message || error) });
    }
  }
  return {
    format: "patent-dom-calibration-v1",
    exported_at: new Date().toISOString(),
    top_origin: new URL(tab.url).origin,
    frames,
    excludes: ["input_values", "cookies", "tokens", "page_body", "business_record_text"],
  };
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  (async () => {
    if (message.type === "get_task") return nativeRequest({ type: "get_ready_task" });
    if (message.type === "get_status") return nativeRequest({ type: "get_status" });
    if (message.type === "start_fill") return startFill();
    if (message.type === "stop_fill") return stopFill();
    if (message.type === "calibrate") return calibration();
    if (message.type === "report_step") return nativeRequest({ ...message.payload, type: "report_step" });
    throw new Error(`未知扩展消息：${message.type}`);
  })()
    .then((payload) => sendResponse({ ok: true, payload }))
    .catch((error) => sendResponse({ ok: false, error: String(error?.message || error) }));
  return true;
});
