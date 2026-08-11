async (page) => {
  await page.reload();
  await page.evaluate(() => {
    window.__nativeReports = [];
    Object.defineProperty(window.chrome, "runtime", {
      configurable: true,
      value: {
        onMessage: {
          addListener(listener) {
            window.__patentListener = listener;
          },
        },
        async sendMessage(message) {
          window.__nativeReports.push(message);
          return { ok: true, payload: {} };
        },
      },
    });
    window.__saveClicks = 0;
    window.__deleteClicks = 0;
    document.querySelector("#save-button").addEventListener(
      "click",
      () => { window.__saveClicks += 1; },
      true,
    );
    document.addEventListener(
      "click",
      (event) => { if (event.target.closest?.(".delete-row")) window.__deleteClicks += 1; },
      true,
    );
  });
  await page.addScriptTag({
    path: "C:/Users/27512/Documents/版权信息自动识别填报/edge_extension/content.js",
  });

  const profile = {
    version: "dom-poc-v1",
    page_fingerprint: {
      required_markers: ["专利信息库", "基本信息"],
      error_markers: ["错误页面", "页面加载中", "校验失败", "弹窗遮挡"],
    },
    fields: {
      patent_no: { kind: "text", labels: ["专利号"], selectors: ["[data-m2-id='patent_no']"] },
      application_title: { kind: "text", labels: ["申请名称"], selectors: ["[data-m2-id='application_title']"] },
      patent_type: { kind: "radio", labels: ["申请类型"], selectors: ["[data-m2-id='patent_type']"] },
      application_date: { kind: "date", labels: ["申请受理日"], selectors: ["[data-m2-id='application_date']"] },
      grant_date: { kind: "date", labels: ["授权公告日"], selectors: ["[data-m2-id='grant_date']"] },
      joint_application: { kind: "checkbox", labels: ["联合申请"], selectors: ["[data-m2-id='joint_application']"] },
      rights_holder_rows: {
        kind: "table",
        labels: ["专利权人表"],
        selectors: ["[data-m2-id='rights_holder_rows']"],
        table: {
          row_selector: "tbody tr:not(.empty-row)", value_selector: "td:nth-child(2) input",
          add_selector: "[data-m2-id='rights_holder_add']", new_input_selector: "tbody tr:last-child td:nth-child(2) input",
        },
      },
      inventor_rows: {
        kind: "table",
        labels: ["发明人（含第一发明人）"],
        selectors: ["[data-m2-id='inventor_rows']"],
        table: {
          row_selector: "tbody tr:not(.empty-row)", value_selector: "td:nth-child(2) input",
          add_selector: "[data-m2-id='inventor_add']", new_input_selector: "tbody tr:last-child td:nth-child(2) input",
        },
      },
      first_inventor_select: {
        kind: "person",
        labels: ["第一发明人"],
        selectors: ["[data-m2-id='first_inventor_select']"],
        person: {
          search_selector: "#person-search", result_selector: "#people-list .person",
          choose_selector: "[data-person]", readback_selector: "#selected-person", readback_prefix: "已选择：",
        },
      },
      patentee_merge: { kind: "text", labels: ["专利权人合并"], selectors: ["[data-m2-id='patentee_merge']"] },
      inventor_merge: { kind: "text", labels: ["发明人合并"], selectors: ["[data-m2-id='inventor_merge']"] },
    },
  };
  const task = {
    task_id: "playwright-local",
    profile_version: "dom-poc-v1",
    fields: [
      { field_id: "patent_no", kind: "text", value: "2020104300960", confirmed: true, normalizer: "patent_no", overwrite_policy: "empty_or_same" },
      { field_id: "application_title", kind: "text", value: "一种测试专利", confirmed: true, normalizer: "trim", overwrite_policy: "empty_or_same" },
      { field_id: "patent_type", kind: "radio", value: "发明", confirmed: true, normalizer: "trim", overwrite_policy: "empty_or_same" },
      { field_id: "application_date", kind: "date", value: "2020-05-01", confirmed: true, normalizer: "date", overwrite_policy: "empty_or_same" },
      { field_id: "grant_date", kind: "date", value: "2024-06-02", confirmed: true, normalizer: "date", overwrite_policy: "empty_or_same" },
      { field_id: "joint_application", kind: "checkbox", value: true, confirmed: true, normalizer: "boolean", overwrite_policy: "empty_or_same" },
    ],
  };

  const inspect = await page.evaluate(
    (value) => new Promise((resolve) => window.__patentListener({ type: "inspect_profile", profile: value }, null, resolve)),
    profile,
  );
  const fill = await page.evaluate(
    (value) => new Promise((resolve) => window.__patentListener({ type: "fill_task", task: value.task, profile: value.profile }, null, resolve)),
    { task, profile },
  );
  const values = await page.evaluate(() => ({
    patent_no: document.querySelector("[data-m2-id='patent_no']").value,
    title: document.querySelector("[data-m2-id='application_title']").value,
    patent_type: document.querySelector("input[name='patent-type']:checked").value,
    application_date: document.querySelector("[data-m2-id='application_date']").value,
    grant_date: document.querySelector("[data-m2-id='grant_date']").value,
    joint: document.querySelector("[data-m2-id='joint_application']").checked,
    reports: window.__nativeReports.length,
    save_clicks: window.__saveClicks,
  }));

  await page.click("#blank-record");
  await page.evaluate(() => { window.__nativeReports = []; });
  const complexTask = {
    task_id: "playwright-complex",
    profile_version: "dom-poc-v1",
    fields: [
      { field_id: "rights_holder_rows", kind: "table", value: ["测试公司甲", "测试公司乙"], confirmed: true, normalizer: "list", overwrite_policy: "empty_or_same" },
      { field_id: "inventor_rows", kind: "table", value: ["张三", "李四"], confirmed: true, normalizer: "list", overwrite_policy: "empty_or_same" },
      { field_id: "first_inventor_select", kind: "person", value: "张三", confirmed: true, normalizer: "trim", overwrite_policy: "empty_or_same" },
      { field_id: "patentee_merge", kind: "text", value: "测试公司甲；测试公司乙", confirmed: true, normalizer: "merged_list", overwrite_policy: "empty_or_same" },
      { field_id: "inventor_merge", kind: "text", value: "张三；李四", confirmed: true, normalizer: "merged_list", overwrite_policy: "empty_or_same" },
    ],
  };
  const complexFill = await page.evaluate(
    (value) => new Promise((resolve) => window.__patentListener({ type: "fill_task", task: value.task, profile: value.profile }, null, resolve)),
    { task: complexTask, profile },
  );
  const complexValues = await page.evaluate(() => ({
    rights_holders: [...document.querySelectorAll("[data-m2-id='rights_holder_rows'] tbody input")].map((item) => item.value),
    inventors: [...document.querySelectorAll("[data-m2-id='inventor_rows'] tbody input")].map((item) => item.value),
    first_inventor: document.querySelector("#selected-person").textContent,
    patentee_merge: document.querySelector("[data-m2-id='patentee_merge']").value,
    inventor_merge: document.querySelector("[data-m2-id='inventor_merge']").value,
    reports: window.__nativeReports.length,
    save_clicks: window.__saveClicks,
    delete_clicks: window.__deleteClicks,
  }));

  await page.click("#conflict-record");
  await page.evaluate(() => { window.__nativeReports = []; });
  const conflictBefore = await page.locator("[data-m2-id='patent_no']").inputValue();
  const conflict = await page.evaluate(
    (value) => new Promise((resolve) => window.__patentListener({ type: "fill_task", task: value.task, profile: value.profile }, null, resolve)),
    { task, profile },
  );
  const conflictAfter = await page.locator("[data-m2-id='patent_no']").inputValue();
  const calibration = await page.evaluate(
    () => new Promise((resolve) => window.__patentListener({ type: "calibrate" }, null, resolve)),
  );
  await page.click("[data-state='error']");
  await page.evaluate(() => { window.__nativeReports = []; });
  const errorPage = await page.evaluate(
    (value) => new Promise((resolve) => window.__patentListener({ type: "fill_task", task: value.task, profile: value.profile }, null, resolve)),
    { task, profile },
  );
  const errorReports = await page.evaluate(() => window.__nativeReports.length);
  await page.click("[data-state='ready']");
  await page.evaluate(() => {
    const duplicate = document.createElement("input");
    duplicate.setAttribute("data-m2-id", "patent_no");
    document.querySelector("[data-m2-id='patent_no']").parentElement.appendChild(duplicate);
    window.__nativeReports = [];
  });
  const duplicateTarget = await page.evaluate(
    (value) => new Promise((resolve) => window.__patentListener({ type: "fill_task", task: value.task, profile: value.profile }, null, resolve)),
    { task, profile },
  );
  const duplicateReports = await page.evaluate(() => window.__nativeReports.length);
  const finalState = await page.evaluate(() => ({
    reports: window.__nativeReports.length,
    save_clicks: window.__saveClicks,
  }));

  return {
    inspect,
    fill,
    values,
    complex: { result: complexFill, values: complexValues },
    conflict: { result: conflict, before: conflictBefore, after: conflictAfter },
    calibration: {
      values_exported: calibration.values_exported,
      page_text_exported: calibration.page_text_exported,
      control_count: calibration.controls.length,
    },
    error_page: { result: errorPage, reports: errorReports },
    duplicate_target: { result: duplicateTarget, reports: duplicateReports },
    final_state: finalState,
  };
}
