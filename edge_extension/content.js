"use strict";

(() => {
  if (globalThis.__patentAutofillContentLoaded) return;
  globalThis.__patentAutofillContentLoaded = true;
  globalThis.__patentAutofillStopRequested = false;

  const SAFE_LABELS = new Set([
    "专利信息库", "基本信息", "专利号", "申请名称", "专利名称", "申请类型", "专利类型",
    "申请受理日", "专利申请日", "授权公告日", "授权日", "联合申请", "技术摘要", "提高效率",
    "提高可靠性", "降低能耗", "专利权人", "发明人", "第一发明人", "身份证号", "联系方式",
    "申请 PCT 专利数量在 5 件以上", "经办人姓名", "经办人手机", "经办人邮箱", "保存", "提交",
    "返回", "删除", "新增权利人", "新增发明人"
  ]);
  const PATENT_TYPE_LABELS = new Set(["发明", "实用新型", "外观设计", "PCT", "巴黎公约", "非核心", "非高价值专利"]);
  const SAFE_DATA_ATTRIBUTES = new Set(["data-testid", "data-test", "data-field", "data-name", "data-m2-id", "data-m2-anchor"]);
  const BLOCKED_LABELS = new Set(["保存", "提交", "返回", "删除", "新建下一条记录"]);

  function normalizeText(value) {
    return String(value ?? "").replace(/\s+/g, " ").trim();
  }

  function normalizeValue(value, normalizer) {
    if (normalizer === "list") {
      const items = Array.isArray(value) ? value : [];
      return JSON.stringify(items.map(normalizeText));
    }
    const text = normalizeText(value);
    if (normalizer === "patent_no") return text.toUpperCase().replace(/^ZL/i, "").replace(/[.\s]/g, "");
    if (normalizer === "date") return text.replace(/[/.]/g, "-");
    if (normalizer === "boolean") return ["true", "1", "yes", "是"].includes(text.toLowerCase()) ? "true" : "false";
    if (normalizer === "merged_list") return text.split(/[；;、,]+/).map(normalizeText).filter(Boolean).join("；");
    return text;
  }

  function visible(element) {
    if (!(element instanceof Element)) return false;
    const style = getComputedStyle(element);
    return style.display !== "none" && style.visibility !== "hidden" && !element.hasAttribute("hidden") && element.getClientRects().length > 0;
  }

  function deepRoots(root = document) {
    const roots = [root];
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      if (node.shadowRoot && node.shadowRoot.mode === "open") roots.push(...deepRoots(node.shadowRoot));
    }
    return roots;
  }

  function queryAll(selector) {
    const found = [];
    for (const root of deepRoots()) {
      try { found.push(...root.querySelectorAll(selector)); } catch (_error) { /* invalid candidate */ }
    }
    return found;
  }

  function dedupe(elements) {
    return [...new Set(elements)].filter((element) => element instanceof Element && visible(element));
  }

  function exactTextElements(text) {
    const target = normalizeText(text);
    return queryAll("label,legend,th,td,span,div,p,h1,h2,h3,h4,button")
      .filter((element) => normalizeText(element.textContent) === target && visible(element));
  }

  function controlsNear(labelElement) {
    const found = [];
    if (labelElement instanceof HTMLLabelElement && labelElement.htmlFor) {
      const escaped = CSS.escape(labelElement.htmlFor);
      found.push(...queryAll(`#${escaped}`));
    }
    if (labelElement.matches("label")) found.push(...labelElement.querySelectorAll("input,textarea,select,[role='textbox'],[role='checkbox'],[role='radio']"));
    let current = labelElement;
    for (let depth = 0; current && depth < 4; depth += 1, current = current.parentElement) {
      found.push(...current.querySelectorAll("input,textarea,select,[contenteditable='true'],[role='textbox'],[role='checkbox'],[role='radio']"));
      if (found.length) break;
    }
    return found;
  }

  function choiceControls(element) {
    if (!(element instanceof Element)) return [];
    const uniqueByOption = (items) => {
      const seen = new Set();
      return items.filter((item) => {
        const owner = item.closest("table[id^='checkboxfield-'],table[id^='radiofield-'],label") || item;
        if (seen.has(owner)) return false;
        seen.add(owner);
        return true;
      });
    };
    const controls = [];
    const nativeControls = [];
    if (element.matches("input[type='radio'],input[type='checkbox']")) nativeControls.push(element);
    nativeControls.push(...element.querySelectorAll("input[type='radio'],input[type='checkbox']"));
    if (nativeControls.length) return uniqueByOption([...new Set(nativeControls)]);
    if (element.matches("[role='radio'],[role='checkbox']")) controls.push(element);
    controls.push(...element.querySelectorAll("[role='radio'],[role='checkbox']"));
    if (controls.length) return uniqueByOption([...new Set(controls)]);
    controls.push(...element.querySelectorAll("table[id^='checkboxfield-'],table[id^='radiofield-']"));
    return [...new Set(controls)];
  }

  function choiceTexts(element) {
    if (!(element instanceof Element)) return [];
    const texts = [
      element.getAttribute("aria-label"),
      element.getAttribute("data-label"),
      element.value,
      element.labels?.[0]?.textContent,
      element.closest("label")?.textContent,
      element.closest("table[id^='checkboxfield-'],table[id^='radiofield-']")?.textContent,
      element.matches("table[id^='checkboxfield-'],table[id^='radiofield-']") ? element.textContent : null,
    ];
    return [...new Set(texts.map(normalizeText).filter(Boolean))];
  }

  function choiceSelected(element) {
    if (!(element instanceof Element)) return false;
    if (element.checked || element.getAttribute("aria-checked") === "true") return true;
    const nested = element.querySelector("input[type='radio'],input[type='checkbox'],[role='radio'],[role='checkbox']");
    if (nested?.checked || nested?.getAttribute("aria-checked") === "true") return true;
    const classChecked = (item) => /(?:^|[-_ ])(?:checked|selected)(?:$|[-_ ])/i.test(String(item.className || ""));
    return classChecked(element) || [...element.querySelectorAll("*")].some(classChecked);
  }

  function choiceInput(element) {
    if (!(element instanceof Element)) return null;
    if (element.matches("input[type='radio'],input[type='checkbox']")) return element;
    return element.querySelector("input[type='radio'],input[type='checkbox']");
  }

  function choiceClickTarget(element) {
    if (!(element instanceof Element)) return null;
    return element.querySelector(
      "input.x-form-checkbox,input.x-form-radio,.x-form-checkbox,.x-form-radio,label,[role='checkbox'],[role='radio'],[role='button']",
    ) || element;
  }

  function triggerChoiceInteraction(element, desired) {
    const targets = [
      element.querySelector(".x-form-checkbox,.x-form-radio"),
      element.querySelector(".x-form-cb-label,.x-form-cb-label-after"),
      choiceClickTarget(element),
    ].filter(Boolean);
    for (const target of [...new Set(targets)]) {
      for (const eventName of ["mousedown", "mouseup", "click"]) {
        target.dispatchEvent(new MouseEvent(eventName, { bubbles: true, cancelable: true, composed: true, view: window }));
        if (choiceSelected(element) === Boolean(desired)) return true;
      }
    }
    return choiceSelected(element) === Boolean(desired);
  }

  function setChoiceState(element, desired) {
    const input = choiceInput(element);
    if (input) {
      if (choiceSelected(input) === Boolean(desired)) return;
      input.click();
      if (choiceSelected(input) === Boolean(desired)) return;
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "checked")?.set;
      if (setter) setter.call(input, Boolean(desired)); else input.checked = Boolean(desired);
      dispatch(input);
      return;
    }
    if (choiceSelected(element) !== Boolean(desired)) {
      triggerChoiceInteraction(element, desired);
    }
  }

  function choiceText(element) {
    const texts = choiceTexts(element);
    return texts.find((text) => PATENT_TYPE_LABELS.has(text))
      || texts.find((text) => text !== normalizeText(element?.value) && !["on", "true", "false", "1", "0"].includes(text.toLowerCase()))
      || texts[0]
      || "";
  }

  function locate(fieldProfile) {
    const found = [];
    for (const selector of fieldProfile.selectors || []) found.push(...queryAll(selector));
    for (const label of fieldProfile.labels || []) {
      found.push(...queryAll(`[aria-label="${CSS.escape(label)}"]`));
      found.push(...queryAll(`[placeholder="${CSS.escape(label)}"]`));
      for (const labelElement of exactTextElements(label)) found.push(...controlsNear(labelElement));
    }
    const candidates = dedupe(found);
    if (fieldProfile.kind === "radio") {
      const groups = candidates.filter((item) => item.matches?.("table[id^='crgroup-']:not([id*='-innerCt'])") || choiceControls(item).length > 0);
      if (groups.length) return dedupe(groups);
      const radios = candidates.filter((item) => item.matches?.("input[type='radio'],input[type='checkbox'],[role='radio'],[role='checkbox']"));
      if (radios.length) return dedupe(radios.map((item) => item.closest("fieldset,.field,[role='radiogroup']") || item));
    }
    if (fieldProfile.kind === "table") {
      return candidates.filter((item) => item.matches("table,[role='grid']"));
    }
    if (fieldProfile.kind === "person") {
      return candidates.filter((item) => item.matches("button,[role='button']"));
    }
    return candidates.filter((item) => {
      if (fieldProfile.kind === "checkbox") return item.matches("input[type='checkbox'],[role='checkbox']") || item.querySelector?.("input[type='checkbox'],[role='checkbox']");
      return item.matches("input,textarea,select,[contenteditable='true'],[role='textbox']");
    });
  }

  function safePage(profile) {
    const bodyText = normalizeText(document.body?.innerText || "");
    const title = normalizeText(document.title);
    const missing = (profile.page_fingerprint?.required_markers || []).filter((marker) => !bodyText.includes(marker) && !title.includes(marker));
    const stateElements = queryAll("[role='alert'],[role='dialog'],.state-banner,.error-message,.loading-message,.validation-message");
    const errors = (profile.page_fingerprint?.error_markers || []).filter((marker) => {
      const exact = exactTextElements(marker).some((element) => !element.matches("button") && visible(element));
      const state = stateElements.some((element) => visible(element) && normalizeText(element.textContent).includes(marker));
      return exact || state;
    });
    return { safe: missing.length === 0 && errors.length === 0, missing, errors };
  }

  function elementValue(element, kind, targetValue, fieldProfile) {
    if (kind === "table") {
      const table = fieldProfile?.table;
      if (!table?.row_selector || !table?.value_selector) throw new Error("table_profile_incomplete");
      return [...element.querySelectorAll(table.row_selector)].map((row) => {
        const values = dedupe([...row.querySelectorAll(table.value_selector)]);
        if (values.length !== 1) throw new Error("table_row_value_not_unique");
        return elementValue(values[0], "text", "", null);
      });
    }
    if (kind === "person") {
      const selector = fieldProfile?.person?.readback_selector;
      if (!selector) throw new Error("person_profile_incomplete");
      const readbacks = dedupe(queryAll(selector));
      if (readbacks.length !== 1) throw new Error("person_readback_not_unique");
      let value = normalizeText(readbacks[0].textContent);
      const prefix = normalizeText(fieldProfile.person.readback_prefix || "");
      if (prefix && value.startsWith(prefix)) value = normalizeText(value.slice(prefix.length));
      return value;
    }
    if (kind === "checkbox") {
      const box = element.matches("input[type='checkbox']") ? element : element.querySelector("input[type='checkbox']");
      return Boolean(box?.checked);
    }
    if (kind === "radio") {
      const radios = choiceControls(element);
      const selected = radios.filter(choiceSelected);
      if (selected.length > 1) return selected.map(choiceText).filter(Boolean).join("；");
      const active = selected[0];
      return active ? choiceText(active) : "";
    }
    if (element instanceof HTMLSelectElement) return normalizeText(element.selectedOptions[0]?.textContent || element.value);
    if (element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement) return element.value;
    return normalizeText(element.textContent);
  }

  function dispatch(element) {
    for (const eventName of ["input", "change", "blur"]) {
      element.dispatchEvent(new Event(eventName, { bubbles: true, composed: true }));
    }
  }

  function emptyCurrentValue(value, kind) {
    if (kind === "checkbox") return value === false;
    if (kind === "table") return Array.isArray(value) && value.length === 0;
    const text = normalizeText(value);
    return !text || ["请选择", "未选择", "尚未选择"].includes(text) || text.startsWith("尚未选择");
  }

  function nativeSetValue(element, value) {
    const prototype = element instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(prototype, "value")?.set;
    if (setter) setter.call(element, value);
    else element.value = value;
    dispatch(element);
  }

  async function setTableField(element, field, fieldProfile) {
    const expected = Array.isArray(field.value) ? field.value.map(normalizeText) : null;
    const tableProfile = fieldProfile.table;
    if (!expected || !tableProfile?.add_selector || !tableProfile?.new_input_selector) {
      throw new Error("table_profile_incomplete");
    }
    let current = elementValue(element, "table", field.value, fieldProfile).map(normalizeText);
    for (let index = current.length; index < expected.length; index += 1) {
      const addButtons = dedupe(queryAll(tableProfile.add_selector));
      if (addButtons.length !== 1) throw new Error("table_add_control_not_unique");
      if (BLOCKED_LABELS.has(normalizeText(addButtons[0].textContent))) throw new Error("destructive_target_blocked");
      addButtons[0].click();
      await new Promise((resolve) => setTimeout(resolve, 80));
      const tables = locate(fieldProfile);
      if (tables.length !== 1) throw new Error("table_remounted_not_unique");
      element = tables[0];
      const newInputs = dedupe([...element.querySelectorAll(tableProfile.new_input_selector)]);
      if (newInputs.length !== 1) throw new Error("table_new_input_not_unique");
      if (!(newInputs[0] instanceof HTMLInputElement || newInputs[0] instanceof HTMLTextAreaElement)) {
        throw new Error("table_new_input_unsupported");
      }
      nativeSetValue(newInputs[0], expected[index]);
      await new Promise((resolve) => setTimeout(resolve, 80));
      current = elementValue(element, "table", field.value, fieldProfile).map(normalizeText);
      if (current.length !== index + 1 || current.some((value, row) => value !== expected[row])) {
        throw new Error("table_incremental_readback_mismatch");
      }
    }
  }

  async function setPersonField(element, field, fieldProfile) {
    const person = fieldProfile.person;
    if (!person?.search_selector || !person?.result_selector || !person?.choose_selector || !person?.readback_selector) {
      throw new Error("person_profile_incomplete");
    }
    element.click();
    await new Promise((resolve) => setTimeout(resolve, 80));
    const searches = dedupe(queryAll(person.search_selector));
    if (searches.length !== 1 || !(searches[0] instanceof HTMLInputElement || searches[0] instanceof HTMLTextAreaElement)) {
      throw new Error("person_search_not_unique");
    }
    const expected = normalizeText(field.value);
    nativeSetValue(searches[0], expected);
    await new Promise((resolve) => setTimeout(resolve, 100));
    const matchingRows = dedupe(queryAll(person.result_selector)).filter((row) => {
      const buttons = dedupe([...row.querySelectorAll(person.choose_selector)]);
      return buttons.some((button) => normalizeText(button.getAttribute("data-person") || "") === expected)
        || normalizeText(row.textContent).startsWith(expected);
    });
    if (matchingRows.length !== 1) throw new Error("person_result_not_unique");
    const chooseButtons = dedupe([...matchingRows[0].querySelectorAll(person.choose_selector)])
      .filter((button) => {
        const explicit = normalizeText(button.getAttribute("data-person") || "");
        return !explicit || explicit === expected;
      });
    if (chooseButtons.length !== 1) throw new Error("person_choose_control_not_unique");
    chooseButtons[0].click();
    await new Promise((resolve) => setTimeout(resolve, 100));
  }

  async function setField(element, field, fieldProfile) {
    if (field.kind === "table") {
      await setTableField(element, field, fieldProfile);
      return;
    }
    if (field.kind === "person") {
      await setPersonField(element, field, fieldProfile);
      return;
    }
    if (field.kind === "checkbox") {
      const box = element.matches("input[type='checkbox']") ? element : element.querySelector("input[type='checkbox']");
      if (!box) throw new Error("checkbox_not_found");
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "checked")?.set;
      if (setter) setter.call(box, Boolean(field.value)); else box.checked = Boolean(field.value);
      dispatch(box);
      return;
    }
    if (field.kind === "radio") {
      const radios = choiceControls(element);
      const expected = normalizeText(field.value);
      const matches = [];
      const seen = new Set();
      for (const radio of radios) {
        if (!choiceTexts(radio).some((text) => text === expected)) continue;
        const key = choiceText(radio) || radio;
        if (!seen.has(key)) {
          seen.add(key);
          matches.push(radio);
        }
      }
      if (matches.length !== 1) throw new Error("radio_option_not_unique");
      const target = matches[0];
      for (const radio of radios) setChoiceState(radio, radio === target);
      return;
    }
    if (field.kind === "select" || element instanceof HTMLSelectElement) {
      const options = [...element.options].filter((option) => normalizeText(option.textContent) === normalizeText(field.value));
      if (options.length !== 1) throw new Error("select_option_not_unique");
      element.value = options[0].value;
      dispatch(element);
      return;
    }
    if (element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement) {
      nativeSetValue(element, String(field.value));
      return;
    }
    if (element.getAttribute("contenteditable") === "true") {
      element.textContent = String(field.value);
      dispatch(element);
      return;
    }
    throw new Error("unsupported_dom_control");
  }

  function tableIsExpectedPrefix(before, expected, normalizer) {
    if (!Array.isArray(before) || !Array.isArray(expected) || before.length > expected.length) return false;
    return before.every((value, index) => normalizeValue(value, normalizer === "list" ? "trim" : normalizer)
      === normalizeValue(expected[index], normalizer === "list" ? "trim" : normalizer));
  }

  async function report(task, field, payload) {
    const response = await chrome.runtime.sendMessage({
      type: "report_step",
      payload: { task_id: task.task_id, field_id: field.field_id, ...payload },
    });
    if (!response?.ok) throw new Error(response?.error || "本机组件未接受字段结果。");
  }

  async function fillTask(task, profile) {
    globalThis.__patentAutofillStopRequested = false;
    if (task.profile_version !== profile.version) return { completed: false, message: "profile_version_mismatch" };
    const page = safePage(profile);
    if (!page.safe) return { completed: false, message: `page_not_safe:${[...page.missing, ...page.errors].join(",")}` };
    for (const field of task.fields) {
      if (globalThis.__patentAutofillStopRequested) return { completed: false, message: "user_stopped" };
      if (field.confirmed !== true || !["empty_or_same", "reviewed_value"].includes(field.overwrite_policy)) {
        return { completed: false, message: `unsafe_field:${field.field_id}` };
      }
      const allowOverwrite = field.overwrite_policy === "reviewed_value";
      const fieldProfile = profile.fields?.[field.field_id];
      if (!fieldProfile || fieldProfile.kind !== field.kind) return { completed: false, message: `profile_field_mismatch:${field.field_id}` };
      const controls = locate(fieldProfile);
      if (controls.length !== 1) return { completed: false, message: `field_not_unique:${field.field_id}:${controls.length}` };
      const control = controls[0];
      if (BLOCKED_LABELS.has(normalizeText(control.textContent)) || control.closest("button") && BLOCKED_LABELS.has(normalizeText(control.closest("button").textContent))) {
        return { completed: false, message: `destructive_target_blocked:${field.field_id}` };
      }
      const before = elementValue(control, field.kind, field.value, fieldProfile);
      const expected = normalizeValue(field.value, field.normalizer);
      const actualBefore = normalizeValue(before, field.normalizer);
      const safeTablePrefix = field.kind === "table" && tableIsExpectedPrefix(before, field.value, field.normalizer);
      if (field.kind === "table" && !safeTablePrefix) {
        const errorCode = allowOverwrite ? "overwrite_not_supported_table" : "existing_value_conflict";
        await report(task, field, { status: "blocked", before, after: before, verified: false, error_code: errorCode });
        return { completed: false, message: `${errorCode}:${field.field_id}` };
      }
      const overwroteExisting = allowOverwrite && !emptyCurrentValue(before, field.kind) && actualBefore !== expected;
      if (!allowOverwrite && field.kind !== "table" && !emptyCurrentValue(before, field.kind) && actualBefore !== expected) {
        await report(task, field, { status: "blocked", before, after: before, verified: false, error_code: "existing_value_conflict" });
        return { completed: false, message: `existing_value_conflict:${field.field_id}` };
      }
      if (actualBefore === expected) {
        await report(task, field, { status: "unchanged", before, after: before, verified: true, overwrote_existing: false });
        continue;
      }
      try {
        await setField(control, field, fieldProfile);
        await new Promise((resolve) => setTimeout(resolve, 80));
      } catch (error) {
        let failedAfter = before;
        try {
          const current = locate(fieldProfile);
          if (current.length === 1) failedAfter = elementValue(current[0], field.kind, field.value, fieldProfile);
        } catch (_readError) { /* retain the safe pre-write evidence */ }
        await report(task, field, { status: "failed", before, after: failedAfter, verified: false, error_code: String(error?.message || error), overwrote_existing: overwroteExisting });
        return { completed: false, message: `write_failed:${field.field_id}` };
      }
      const afterControls = locate(fieldProfile);
      if (afterControls.length !== 1) {
        await report(task, field, { status: "failed", before, after: before, verified: false, error_code: "field_remounted_not_unique", overwrote_existing: overwroteExisting });
        return { completed: false, message: `write_failed:${field.field_id}` };
      }
      const after = elementValue(afterControls[0], field.kind, field.value, fieldProfile);
      const verified = normalizeValue(after, field.normalizer) === expected;
      await report(task, field, { status: verified ? "filled" : "failed", before, after, verified, error_code: verified ? null : "readback_mismatch", overwrote_existing: overwroteExisting });
      if (!verified) return { completed: false, message: `readback_mismatch:${field.field_id}` };
      await new Promise((resolve) => setTimeout(resolve, 0));
    }
    return { completed: true, field_count: task.fields.length, save_attempted: false };
  }

  function safeAttributeValue(name, value) {
    const text = normalizeText(value);
    if (!text || text.length > 80 || /\d{7,}/.test(text) || /[?&#=/]{2,}/.test(text)) return null;
    if (["type", "role"].includes(name)) return /^[a-zA-Z][a-zA-Z0-9_-]{0,79}$/.test(text) ? text : null;
    if (["id", "name"].includes(name) || SAFE_DATA_ATTRIBUTES.has(name)) {
      return /^[a-zA-Z_][a-zA-Z0-9_.:-]{0,79}$/.test(text) ? text : null;
    }
    if (["aria-label", "placeholder"].includes(name)) return SAFE_LABELS.has(text) ? text : null;
    return null;
  }

  function knownLabel(element) {
    const candidates = [element.getAttribute("aria-label"), element.getAttribute("placeholder")];
    if (element.id) {
      for (const label of queryAll(`label[for="${CSS.escape(element.id)}"]`)) candidates.push(label.textContent);
    }
    const wrapping = element.closest("label");
    if (wrapping) candidates.push(wrapping.textContent);
    return candidates.map(normalizeText).find((item) => SAFE_LABELS.has(item)) || null;
  }

  function safeAttributes(element) {
    const attributes = {};
    for (const attribute of element.attributes || []) {
      const value = safeAttributeValue(attribute.name, attribute.value);
      if (value !== null) attributes[attribute.name] = value;
    }
    return attributes;
  }

  function structuralParent(element) {
    if (element.parentElement) return element.parentElement;
    const root = element.getRootNode();
    return root instanceof ShadowRoot && root.mode === "open" ? root.host : null;
  }

  function safeDomAncestry(element) {
    const ancestry = [];
    let current = element;
    for (let depth = 0; current instanceof Element && depth < 8; depth += 1) {
      ancestry.push({ tag: current.tagName.toLowerCase(), attributes: safeAttributes(current) });
      current = structuralParent(current);
    }
    return ancestry.reverse();
  }

  function calibrateWidgets() {
    return queryAll("table,[role='grid']").filter(visible).map((element) => ({
      tag: element.tagName.toLowerCase(),
      attributes: safeAttributes(element),
      dom_ancestry: safeDomAncestry(element),
      row_count: element.querySelectorAll("tbody tr,[role='row']").length,
      row_structure: [...element.querySelectorAll("tbody tr,[role='row']")].slice(0, 5).map((row) => ({
        tag: row.tagName.toLowerCase(),
        cells: [...row.children].map((cell) => ({
          tag: cell.tagName.toLowerCase(),
          controls: [...cell.querySelectorAll("input,textarea,select,button,[role='textbox'],[role='button']")]
            .map((control) => ({ tag: control.tagName.toLowerCase(), attributes: safeAttributes(control) })),
        })),
      })),
    }));
  }

  function calibrate() {
    const controls = [];
    for (const element of queryAll("input,textarea,select,button,[contenteditable='true'],[role='textbox'],[role='checkbox'],[role='radio']")) {
      if (!visible(element)) continue;
      controls.push({
        tag: element.tagName.toLowerCase(),
        input_type: element.getAttribute("type") || null,
        known_label: knownLabel(element),
        attributes: safeAttributes(element),
        dom_ancestry: safeDomAncestry(element),
        inside_open_shadow: element.getRootNode() instanceof ShadowRoot,
      });
    }
    const iframe_origins = queryAll("iframe").map((frame) => {
      try { return new URL(frame.src, location.href).origin; } catch (_error) { return null; }
    }).filter(Boolean);
    const markers = [...SAFE_LABELS].filter((label) => exactTextElements(label).length > 0);
    return {
      accessible: true,
      origin: location.origin,
      frame_origin: location.origin,
      known_markers: markers,
      controls,
      widgets: calibrateWidgets(),
      iframe_origins: [...new Set(iframe_origins)],
      values_exported: false,
      page_text_exported: false,
    };
  }

  class DomPageAdapter {
    constructor(profile) {
      this.profile = profile;
    }

    inspect() {
      const page = safePage(this.profile);
      const fieldCounts = {};
      for (const [fieldId, fieldProfile] of Object.entries(this.profile.fields || {})) {
        fieldCounts[fieldId] = locate(fieldProfile).length;
      }
      return { ...page, field_counts: fieldCounts, origin: location.origin };
    }

    fill(task) {
      return fillTask(task, this.profile);
    }

    calibrate() {
      return calibrate();
    }
  }

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message.type === "stop") {
      globalThis.__patentAutofillStopRequested = true;
      sendResponse({ stopped: true });
      return false;
    }
    if (message.type === "inspect_profile") {
      sendResponse(new DomPageAdapter(message.profile).inspect());
      return false;
    }
    if (message.type === "calibrate") {
      sendResponse(new DomPageAdapter({ fields: {} }).calibrate());
      return false;
    }
    if (message.type === "fill_task") {
      new DomPageAdapter(message.profile).fill(message.task)
        .then(sendResponse)
        .catch((error) => sendResponse({ completed: false, message: String(error?.message || error) }));
      return true;
    }
    return false;
  });
})();
