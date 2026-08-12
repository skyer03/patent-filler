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
      const tables = candidates.filter((item) => item.matches("table,[role='grid']"));
      const identitySelector = fieldProfile.table?.identity_selector;
      const identityTexts = (fieldProfile.table?.identity_texts || []).map(normalizeText).filter(Boolean);
      if (!identitySelector && identityTexts.length === 0) return tables;
      return tables.filter((item) => {
        const selectorMatch = Boolean(identitySelector && item.querySelector(identitySelector));
        const texts = [...item.querySelectorAll(".x-column-header-text,th")].map((node) => normalizeText(node.textContent));
        const textMatch = identityTexts.length > 0 && identityTexts.every((text) => texts.includes(text));
        return selectorMatch || textMatch;
      });
    }
    if (fieldProfile.kind === "person") {
      if (fieldProfile.person?.mode === "direct") {
        return candidates.filter((item) => item.matches("input,textarea,[contenteditable='true'],[role='textbox']"));
      }
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
        const values = [...new Set(row.querySelectorAll(table.value_selector))];
        if (values.length !== 1) throw new Error("table_row_value_not_unique");
        return elementValue(values[0], "text", "", null);
      });
    }
    if (kind === "person") {
      if (fieldProfile?.person?.mode === "direct") {
        if (element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement) return element.value;
        return normalizeText(element.textContent);
      }
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

  function commitTableEditor(input) {
    const options = {
      bubbles: true,
      cancelable: true,
      composed: true,
      view: window,
      key: "Enter",
      code: "Enter",
      keyCode: 13,
      charCode: 13,
      which: 13,
    };
    input.dispatchEvent(new KeyboardEvent("keydown", options));
    input.dispatchEvent(new KeyboardEvent("keypress", options));
    input.dispatchEvent(new KeyboardEvent("keyup", options));
    input.blur();
  }

  function wait(milliseconds) {
    return new Promise((resolve) => setTimeout(resolve, milliseconds));
  }

  function clickLikeUser(element, doubleClick = false) {
    const rect = element.getBoundingClientRect();
    const clientX = Math.round(rect.left + rect.width / 2);
    const clientY = Math.round(rect.top + rect.height / 2);
    const base = {
      bubbles: true,
      cancelable: true,
      composed: true,
      view: window,
      button: 0,
      clientX,
      clientY,
      screenX: Math.round(window.screenX + clientX),
      screenY: Math.round(window.screenY + clientY),
    };
    if (typeof PointerEvent === "function") {
      element.dispatchEvent(new PointerEvent("pointerdown", {
        ...base, buttons: 1, detail: 0, pointerId: 1, pointerType: "mouse", isPrimary: true,
      }));
    }
    element.dispatchEvent(new MouseEvent("mousedown", { ...base, buttons: 1, detail: 1 }));
    if (typeof PointerEvent === "function") {
      element.dispatchEvent(new PointerEvent("pointerup", {
        ...base, buttons: 0, detail: 0, pointerId: 1, pointerType: "mouse", isPrimary: true,
      }));
    }
    element.dispatchEvent(new MouseEvent("mouseup", { ...base, buttons: 0, detail: 1 }));
    element.dispatchEvent(new MouseEvent("click", { ...base, buttons: 0, detail: 1 }));
    if (doubleClick) element.dispatchEvent(new MouseEvent("dblclick", { ...base, buttons: 0, detail: 2 }));
  }

  function rectanglesOverlap(first, second) {
    const a = first.getBoundingClientRect();
    const b = second.getBoundingClientRect();
    return Math.min(a.right, b.right) - Math.max(a.left, b.left) > 2
      && Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > 2;
  }

  function elementVisible(element) {
    if (!element?.isConnected) return false;
    const style = getComputedStyle(element);
    if (style.display === "none" || style.visibility === "hidden" || style.visibility === "collapse") {
      return false;
    }
    return [...element.getClientRects()].some((rect) => rect.width > 2 && rect.height > 2);
  }

  async function closeVisibleTableEditors(element, tableProfile) {
    const inputs = dedupe([...element.querySelectorAll(tableProfile.new_input_selector)]).filter(elementVisible);
    for (const input of inputs) {
      if (!(input instanceof HTMLInputElement || input instanceof HTMLTextAreaElement)) continue;
      input.focus();
      input.blur();
    }
    if (inputs.length) await wait(80);
    const remaining = dedupe([...element.querySelectorAll(tableProfile.new_input_selector)]).filter(elementVisible);
    if (remaining.length > 0) {
      throw new Error("table_stale_editor_active");
    }
  }

  async function waitForAddedTableRow(fieldProfile, field, tableProfile, prefix) {
    const deadline = Date.now() + 1500;
    while (Date.now() < deadline) {
      const tables = locate(fieldProfile);
      if (tables.length > 1) throw new Error("table_remounted_not_unique");
      if (tables.length === 1) {
        const values = elementValue(tables[0], "table", field.value, fieldProfile).map(normalizeText);
        const sharedLength = Math.min(values.length, prefix.length);
        if (values.slice(0, sharedLength).some((value, row) => value !== prefix[row])) {
          throw new Error("table_existing_row_changed_after_add");
        }
        if (values.length > prefix.length + 1) throw new Error("table_add_row_count_mismatch");
        if (values.length === prefix.length + 1) {
          if (values[prefix.length] !== "") throw new Error("table_new_row_not_blank");
          const rows = dedupe([...tables[0].querySelectorAll(tableProfile.row_selector)]);
          if (rows.length !== values.length) throw new Error("table_row_count_mismatch");
          return { element: tables[0], row: rows[prefix.length] };
        }
      }
      await wait(40);
    }
    throw new Error("table_new_row_not_observed");
  }

  async function activateNewTableInput(element, row, tableProfile) {
    const cells = dedupe([...row.querySelectorAll(tableProfile.new_cell_selector)]);
    if (cells.length !== 1) throw new Error("table_new_cell_not_unique");
    const cell = cells[0];
    cell.scrollIntoView({ block: "nearest", inline: "nearest" });

    for (const doubleClick of [false, true]) {
      clickLikeUser(cell, doubleClick);
      const deadline = Date.now() + 700;
      while (Date.now() < deadline) {
        const visibleInputs = dedupe([...element.querySelectorAll(tableProfile.new_input_selector)]).filter(elementVisible);
        if (visibleInputs.length > 1) throw new Error("table_new_input_not_unique");
        const matching = visibleInputs.filter((input) => rectanglesOverlap(input, cell));
        if (matching.length === 1) return matching[0];
        await wait(40);
      }
    }
    throw new Error("table_editor_not_on_new_row");
  }

  async function waitForTableValues(fieldProfile, field, expected, errorPrefix = "table_incremental") {
    const deadline = Date.now() + 1500;
    while (Date.now() < deadline) {
      const tables = locate(fieldProfile);
      if (tables.length > 1) throw new Error("table_remounted_not_unique");
      if (tables.length === 1) {
        const values = elementValue(tables[0], "table", field.value, fieldProfile).map(normalizeText);
        if (values.length > expected.length) throw new Error(`${errorPrefix}_row_count_mismatch`);
        if (values.length === expected.length && values.every((value, row) => value === expected[row])) {
          return { element: tables[0], values };
        }
      }
      await wait(40);
    }
    throw new Error(`${errorPrefix}_readback_mismatch`);
  }

  async function overwriteExistingTableRows(element, field, fieldProfile, expected, current) {
    const tableProfile = fieldProfile.table;
    if (current.length > expected.length) throw new Error("table_overwrite_requires_delete");
    for (let index = 0; index < current.length; index += 1) {
      if (current[index] === expected[index]) continue;
      await closeVisibleTableEditors(element, tableProfile);
      const rows = dedupe([...element.querySelectorAll(tableProfile.row_selector)]);
      if (rows.length !== current.length) throw new Error("table_overwrite_row_count_changed");
      const editor = await activateNewTableInput(element, rows[index], tableProfile);
      if (!(editor instanceof HTMLInputElement || editor instanceof HTMLTextAreaElement)) {
        throw new Error("table_existing_input_unsupported");
      }
      editor.focus();
      nativeSetValue(editor, expected[index]);
      commitTableEditor(editor);
      const next = [...current];
      next[index] = expected[index];
      const committed = await waitForTableValues(fieldProfile, field, next, "table_overwrite");
      element = committed.element;
      current = committed.values;
    }
    return { element, current };
  }

  async function setTableField(element, field, fieldProfile, allowOverwrite = false) {
    const expected = Array.isArray(field.value) ? field.value.map(normalizeText) : null;
    const tableProfile = fieldProfile.table;
    if (!expected || !tableProfile?.add_selector || !tableProfile?.row_selector
      || !tableProfile?.new_cell_selector || !tableProfile?.new_input_selector) {
      throw new Error("table_profile_incomplete");
    }
    let current = elementValue(element, "table", field.value, fieldProfile).map(normalizeText);
    if (allowOverwrite) {
      const overwritten = await overwriteExistingTableRows(element, field, fieldProfile, expected, current);
      element = overwritten.element;
      current = overwritten.current;
    }
    for (let index = current.length; index < expected.length; index += 1) {
      await closeVisibleTableEditors(element, tableProfile);
      const addCandidates = tableProfile.add_within_table
        ? [...element.querySelectorAll(tableProfile.add_selector)]
        : queryAll(tableProfile.add_selector);
      const addButtons = dedupe(addCandidates);
      if (addButtons.length !== 1) throw new Error("table_add_control_not_unique");
      if (BLOCKED_LABELS.has(normalizeText(addButtons[0].textContent))) throw new Error("destructive_target_blocked");
      clickLikeUser(addButtons[0]);
      const added = await waitForAddedTableRow(fieldProfile, field, tableProfile, current);
      element = added.element;
      const newInput = await activateNewTableInput(element, added.row, tableProfile);
      if (!(newInput instanceof HTMLInputElement || newInput instanceof HTMLTextAreaElement)) {
        throw new Error("table_new_input_unsupported");
      }
      newInput.focus();
      nativeSetValue(newInput, expected[index]);
      commitTableEditor(newInput);
      const committed = await waitForTableValues(fieldProfile, field, expected.slice(0, index + 1));
      element = committed.element;
      current = committed.values;
    }
  }

  async function setPersonField(element, field, fieldProfile) {
    const person = fieldProfile.person;
    if (person?.mode === "direct") {
      if (!(element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement)) {
        throw new Error("person_direct_input_unsupported");
      }
      element.focus();
      nativeSetValue(element, normalizeText(field.value));
      element.blur();
      return;
    }
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

  async function setField(element, field, fieldProfile, allowOverwrite = false) {
    if (field.kind === "table") {
      await setTableField(element, field, fieldProfile, allowOverwrite);
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
      if (field.kind === "table" && !safeTablePrefix && !allowOverwrite) {
        const errorCode = "existing_value_conflict";
        await report(task, field, { status: "blocked", before, after: before, verified: false, error_code: errorCode });
        return { completed: false, message: `${errorCode}:${field.field_id}` };
      }
      if (field.kind === "table" && allowOverwrite && before.length > field.value.length) {
        const errorCode = "table_overwrite_requires_delete";
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
        await setField(control, field, fieldProfile, allowOverwrite);
        await new Promise((resolve) => setTimeout(resolve, 80));
      } catch (error) {
        let failedAfter = before;
        const errorCode = String(error?.message || error);
        try {
          const current = locate(fieldProfile);
          if (current.length === 1) failedAfter = elementValue(current[0], field.kind, field.value, fieldProfile);
        } catch (_readError) { /* retain the safe pre-write evidence */ }
        await report(task, field, { status: "failed", before, after: failedAfter, verified: false, error_code: errorCode, overwrote_existing: overwroteExisting });
        return { completed: false, message: `write_failed:${field.field_id}:${errorCode}` };
      }
      const afterControls = locate(fieldProfile);
      if (afterControls.length !== 1) {
        await report(task, field, { status: "failed", before, after: before, verified: false, error_code: "field_remounted_not_unique", overwrote_existing: overwroteExisting });
        return { completed: false, message: `write_failed:${field.field_id}:field_remounted_not_unique` };
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
