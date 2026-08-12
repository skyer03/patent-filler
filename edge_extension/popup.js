"use strict";

const loadButton = document.querySelector("#load");
const startButton = document.querySelector("#start");
const stopButton = document.querySelector("#stop");
const calibrateButton = document.querySelector("#calibrate");
const statusNode = document.querySelector("#status");
const detailsNode = document.querySelector("#details");
let currentTask = null;

function showStatus(message, kind = "") {
  statusNode.textContent = message;
  statusNode.className = kind;
}

function safeTaskSummary(task) {
  return {
    task_id: task.task_id,
    status: task.status,
    profile_version: task.profile_version,
    overwrite_existing: task.safety?.overwrite_existing === true,
    fields: task.fields.map((field) => ({
      field_id: field.field_id,
      kind: field.kind,
      source: field.source,
      confirmed: field.confirmed,
      overwrite_policy: field.overwrite_policy,
    })),
    save_submit_return_delete: "manual_only",
  };
}

async function request(type) {
  const response = await chrome.runtime.sendMessage({ type });
  if (!response?.ok) throw new Error(response?.error || "扩展后台返回未知错误。");
  return response.payload;
}

loadButton.addEventListener("click", async () => {
  startButton.disabled = true;
  showStatus("正在读取本机已审核任务……");
  try {
    currentTask = await request("get_task");
    detailsNode.textContent = JSON.stringify(safeTaskSummary(currentTask), null, 2);
    showStatus(`任务已就绪：${currentTask.fields.length} 个已确认字段。`, "success");
    startButton.disabled = false;
  } catch (error) {
    currentTask = null;
    detailsNode.textContent = "";
    showStatus(String(error?.message || error), "error");
  }
});

startButton.addEventListener("click", async () => {
  if (!currentTask) return;
  startButton.disabled = true;
  showStatus("正在检查页面并逐字段预填；请勿刷新或切换当前标签页……");
  try {
    const result = await request("start_fill");
    detailsNode.textContent = JSON.stringify(result, null, 2);
    showStatus("预填完成。请人工检查网页内容，然后手动点击保存。", "success");
  } catch (error) {
    showStatus(`已安全停止：${String(error?.message || error)}`, "error");
  }
});

stopButton.addEventListener("click", async () => {
  showStatus("正在停止当前任务……");
  try {
    const result = await request("stop_fill");
    detailsNode.textContent = JSON.stringify(result, null, 2);
    showStatus("任务已停止，后续字段不会继续执行。", "error");
  } catch (error) {
    showStatus(String(error?.message || error), "error");
  }
  startButton.disabled = true;
});

calibrateButton.addEventListener("click", async () => {
  showStatus("正在生成不含输入值和页面正文的 DOM 校准文件……");
  try {
    const value = await request("calibrate");
    const blob = new Blob([JSON.stringify(value, null, 2) + "\n"], { type: "application/json" });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = `patent-dom-calibration-${new Date().toISOString().replace(/[:.]/g, "-")}.json`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(link.href), 1000);
    detailsNode.textContent = JSON.stringify({ frames: value.frames.length, values_exported: false, page_text_exported: false }, null, 2);
    showStatus("脱敏 DOM 校准文件已导出。请审核后再外发。", "success");
  } catch (error) {
    showStatus(String(error?.message || error), "error");
  }
});
