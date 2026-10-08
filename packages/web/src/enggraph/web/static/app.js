// What the pages cannot do with markup alone: JSON writes to /api, modals,
// drafts kept across a reload, and a select that can be filtered.
(function () {
  "use strict";

  const DRAFT_PREFIX = "draft:";

  // ---- calling the API ----------------------------------------------------

  async function unwrap(response) {
    const text = await response.text();
    let body = null;
    if (text !== "") {
      try {
        body = JSON.parse(text);
      } catch {
        // An HTML body is the gateway's own error page, not an API answer.
        const html = text.trimStart().startsWith("<");
        const status = response.status + " " + response.statusText;
        body = { error: html ? status + ", answered by the gateway" : text };
      }
    }
    if (!response.ok) {
      const message =
        body !== null &&
        typeof body === "object" &&
        typeof body.error === "string"
          ? body.error
          : "Request failed with status " + response.status;
      throw new Error(message);
    }
    return body;
  }

  async function call(method, path, body) {
    const options = { method };
    if (method !== "GET") {
      options.headers = { "Content-Type": "application/json" };
      options.body = JSON.stringify(body === undefined ? {} : body);
    }
    return unwrap(await fetch(path, options));
  }

  function refresh() {
    if (window.htmx === undefined) {
      window.location.reload();
      return Promise.resolve();
    }
    return window.htmx.ajax("GET", window.location.href, {
      target: "#main",
      select: "#main",
      swap: "outerHTML",
    });
  }

  // ---- errors ---------------------------------------------------------------

  function errorBox(message, what) {
    const box = document.createElement("div");
    box.className = "error";
    const title = document.createElement("p");
    title.className = "error-title";
    const strong = document.createElement("strong");
    strong.textContent = "Error";
    title.append(strong, what ? " - " + what : "");
    const pre = document.createElement("pre");
    pre.textContent = message;
    const copy = document.createElement("button");
    copy.type = "button";
    copy.className = "secondary copy";
    copy.dataset.copy = message;
    copy.textContent = "Copy";
    box.append(title, pre, copy);
    return box;
  }

  function slotFor(source) {
    const named = source.dataset.errorIn;
    if (named) {
      return document.querySelector(named);
    }
    const scope = source.closest("form, .modal, section, td, div") || source;
    let slot = scope.querySelector(":scope > [data-error]");
    if (slot === null) {
      slot = document.createElement("div");
      slot.dataset.error = "";
      const row = scope.querySelector(":scope > .row:last-of-type");
      if (row !== null) {
        scope.insertBefore(slot, row);
      } else {
        scope.append(slot);
      }
    }
    return slot;
  }

  function showError(source, message) {
    const slot = slotFor(source);
    if (slot !== null) {
      slot.replaceChildren(errorBox(message, source.dataset.errorWhat));
    }
  }

  function clearError(source) {
    const slot = slotFor(source);
    if (slot !== null) {
      slot.replaceChildren();
    }
  }

  // ---- reading a form into a JSON body --------------------------------------

  function fieldValue(control) {
    const type = control.dataset.type;
    if (control.dataset.headingOf) {
      const titled = control.form.elements.namedItem(control.dataset.headingOf);
      return "# " + (titled === null ? "" : titled.value) + "\n";
    }
    if (control.type === "checkbox") {
      if (control.dataset.inherited !== undefined && !control.dataset.touched) {
        return undefined;
      }
      return control.checked;
    }
    const raw =
      control.dataset.trim === undefined ? control.value : control.value.trim();
    if (type === "number") {
      return raw.trim() === "" ? null : Number(raw);
    }
    if (type === "tristate") {
      return raw === "" ? null : raw === "true";
    }
    if (type === "lines") {
      return raw
        .split("\n")
        .map((line) => line.trim())
        .filter((line) => line !== "");
    }
    if (control.dataset.omitEmpty !== undefined && raw.trim() === "") {
      return undefined;
    }
    if (control.dataset.nullEmpty !== undefined && raw.trim() === "") {
      return null;
    }
    return raw;
  }

  function bodyOf(source) {
    const body = source.dataset.body ? JSON.parse(source.dataset.body) : {};
    if (!(source instanceof HTMLFormElement)) {
      return body;
    }
    for (const control of source.elements) {
      if (
        !control.name ||
        control.disabled ||
        control.dataset.skip !== undefined
      ) {
        continue;
      }
      if (control.type === "radio" && !control.checked) {
        continue;
      }
      const value = fieldValue(control);
      if (value !== undefined) {
        body[control.name] = value;
      }
    }
    return body;
  }

  function fill(template, answer, sent) {
    return template.replace(/\{([^}]+)\}/g, (_whole, paths) => {
      for (const path of paths.split("|")) {
        for (const from of [answer, sent]) {
          let value = from;
          for (const key of path.split(".")) {
            value =
              value === null || value === undefined ? undefined : value[key];
          }
          if (typeof value === "string" || typeof value === "number") {
            return encodeURIComponent(String(value));
          }
        }
      }
      return "";
    });
  }

  async function act(source) {
    const calls = source.dataset.api.split(";").map((one) => one.trim());
    const sent = bodyOf(source);
    const buttons =
      source instanceof HTMLFormElement
        ? [...source.querySelectorAll("button")]
        : [source];
    const disabled = buttons.map((button) => button.disabled);
    buttons.forEach((button) => (button.disabled = true));
    clearError(source);
    try {
      let answer = null;
      for (const one of calls) {
        const [method, ...rest] = one.split(" ");
        answer = await call(method, fill(rest.join(" "), null, sent), sent);
      }
      if (source.dataset.draft) {
        dropDraft(source.dataset.draft);
      }
      const steps = (source.dataset.then || "refresh").split(",");
      for (const then of steps) {
        if (then.startsWith("goto:")) {
          window.location.href = fill(then.slice(5), answer, sent);
          return;
        }
        if (then.startsWith("say:")) {
          const into = document.querySelector(then.slice(4));
          if (into !== null) {
            const said = answer !== null && answer.said;
            into.textContent =
              typeof said === "string" ? said : JSON.stringify(answer, null, 2);
            into.hidden = false;
          }
        } else if (then.startsWith("trigger:")) {
          for (const heard of document.querySelectorAll(then.slice(8))) {
            window.htmx.trigger(heard, "reload");
          }
          const modal = source.closest(".modal-backdrop");
          if (modal !== null) {
            modal.hidden = true;
          }
        } else if (then === "reset" && source instanceof HTMLFormElement) {
          source.reset();
        } else if (then === "refresh") {
          await refresh();
          return;
        }
      }
    } catch (reason) {
      showError(
        source,
        reason instanceof Error ? reason.message : String(reason),
      );
    }
    buttons.forEach((button, index) => (button.disabled = disabled[index]));
    if (source instanceof HTMLFormElement) {
      gate(source);
    }
  }

  // ---- forms that only go once something was typed --------------------------

  function gate(form) {
    const submit = form.querySelector("[type=submit]");
    if (submit === null || submit.dataset.gated === undefined) {
      return;
    }
    let ready = true;
    for (const control of form.elements) {
      const value = control.value === undefined ? "" : control.value;
      if (control.dataset.confirm !== undefined) {
        ready = ready && value === control.dataset.confirm;
      }
      if (control.dataset.required !== undefined) {
        ready = ready && value.trim() !== "";
      }
      if (control.dataset.differs !== undefined) {
        ready = ready && value.trim() !== control.dataset.differs;
      }
    }
    if (form.dataset.draft !== undefined || form.dataset.dirty !== undefined) {
      ready = ready && form.querySelector(".unsaved") !== null;
    }
    submit.disabled = !ready;
  }

  function mirror(control) {
    // Text elsewhere on the page that repeats what is being typed.
    if (!control.name) {
      return;
    }
    const scope = control.closest("form, .modal") || document;
    const marks = scope.querySelectorAll(`[data-mirror="${control.name}"]`);
    for (const mark of marks) {
      const value = control.value.trim();
      mark.textContent =
        mark.dataset.length !== undefined
          ? String(control.value.length)
          : value || mark.dataset.empty || "";
    }
  }

  // ---- drafts ---------------------------------------------------------------

  function loadDraft(key) {
    try {
      const raw = window.sessionStorage.getItem(DRAFT_PREFIX + key);
      return raw === null ? {} : JSON.parse(raw);
    } catch {
      return {};
    }
  }

  function dropDraft(key) {
    try {
      window.sessionStorage.removeItem(DRAFT_PREFIX + key);
    } catch {
      return;
    }
  }

  function storedOf(control) {
    if (control.type === "checkbox") {
      return control.defaultChecked;
    }
    if (control instanceof HTMLSelectElement) {
      const chosen = [...control.options].find((one) => one.defaultSelected);
      return chosen === undefined
        ? control.options[0]?.value || ""
        : chosen.value;
    }
    return control.defaultValue;
  }

  function currentOf(control) {
    return control.type === "checkbox" ? control.checked : control.value;
  }

  function mark(form) {
    const edits = {};
    let dirty = 0;
    for (const control of form.elements) {
      if (!control.name || control.dataset.skip !== undefined) {
        continue;
      }
      const changed =
        currentOf(control) !== storedOf(control) ||
        (control.dataset.inherited !== undefined &&
          control.dataset.touched === "1");
      const shown =
        control.type === "checkbox" ? control.closest("label") : control;
      (shown || control).classList.toggle("unsaved", changed);
      control.classList.toggle("unsaved", changed);
      if (changed) {
        dirty += 1;
        if (control.type !== "password") {
          edits[control.name] = currentOf(control);
        }
      }
    }
    for (const note of form.querySelectorAll("[data-dirty-count]")) {
      note.textContent = String(dirty);
    }
    for (const note of form.querySelectorAll("[data-dirty-only]")) {
      note.hidden = dirty === 0;
    }
    if (form.dataset.draft) {
      try {
        if (Object.keys(edits).length === 0) {
          window.sessionStorage.removeItem(DRAFT_PREFIX + form.dataset.draft);
        } else {
          window.sessionStorage.setItem(
            DRAFT_PREFIX + form.dataset.draft,
            JSON.stringify(edits),
          );
        }
      } catch {
        return;
      }
    }
  }

  function restore(form) {
    const edits = loadDraft(form.dataset.draft);
    for (const control of form.elements) {
      if (!(control.name in edits)) {
        continue;
      }
      if (control.type === "checkbox") {
        control.checked = edits[control.name] === true;
        control.dataset.touched = "1";
      } else {
        control.value = edits[control.name];
      }
      mirror(control);
    }
  }

  function discard(form) {
    form.reset();
    for (const control of form.elements) {
      delete control.dataset.touched;
      mirror(control);
    }
    if (form.dataset.draft) {
      dropDraft(form.dataset.draft);
    }
    mark(form);
    gate(form);
    untest(form);
    for (const select of form.querySelectorAll("select[data-picker]")) {
      syncPicker(select);
    }
  }

  // ---- a select that scrolls inside the window and can be filtered ----------

  const GAP = 4;
  const MARGIN = 16;
  const MIN_WIDTH = 256;
  const MAX_HEIGHT = 384;
  let openPanel = null;

  function closePanel() {
    if (openPanel !== null) {
      openPanel.panel.remove();
      openPanel.trigger.setAttribute("aria-expanded", "false");
      openPanel = null;
    }
  }

  function syncPicker(select) {
    const trigger = select.nextElementSibling;
    if (trigger !== null && trigger.classList.contains("picker-trigger")) {
      const chosen = select.selectedOptions[0];
      trigger.textContent =
        chosen === undefined ? select.value : chosen.textContent;
    }
  }

  function pick(select, trigger, value) {
    closePanel();
    trigger.focus();
    if (value !== select.value) {
      select.value = value;
      syncPicker(select);
      select.dispatchEvent(new Event("input", { bubbles: true }));
      select.dispatchEvent(new Event("change", { bubbles: true }));
    }
  }

  function drawOptions(select, trigger, list, needle) {
    const shown = [...select.options].filter((option) => {
      const group = option.dataset.group || "";
      return (
        needle === "" ||
        option.textContent.toLowerCase().includes(needle) ||
        group.toLowerCase().includes(needle)
      );
    });
    list.replaceChildren();
    if (shown.length === 0) {
      const none = document.createElement("div");
      none.className = "picker-group";
      none.textContent = "Nothing matches.";
      list.append(none);
    }
    shown.forEach((option, index) => {
      const group = option.dataset.group;
      const before = index === 0 ? undefined : shown[index - 1].dataset.group;
      const holder = document.createElement("div");
      if (group !== undefined && (index === 0 || before !== group)) {
        const heading = document.createElement("div");
        heading.className = "picker-group";
        heading.textContent = group;
        holder.append(heading);
      }
      const button = document.createElement("button");
      button.type = "button";
      button.setAttribute("role", "option");
      button.setAttribute(
        "aria-selected",
        String(option.value === select.value),
      );
      button.className =
        group === undefined ? "picker-option" : "picker-option nested";
      button.textContent = option.textContent;
      button.addEventListener("click", () =>
        pick(select, trigger, option.value),
      );
      holder.append(button);
      list.append(holder);
    });
    return shown;
  }

  function openPicker(select, trigger) {
    closePanel();
    const rect = trigger.getBoundingClientRect();
    const below = window.innerHeight - rect.bottom - GAP - MARGIN;
    const above = rect.top - GAP - MARGIN;
    const upward = below < MAX_HEIGHT && above > below;
    const width = Math.max(rect.width, MIN_WIDTH);
    const panel = document.createElement("div");
    panel.className = "picker-panel";
    const left = Math.min(rect.left, window.innerWidth - width - MARGIN);
    panel.style.left = Math.max(MARGIN, left) + "px";
    panel.style.width = width + "px";
    panel.style.maxHeight = Math.min(MAX_HEIGHT, upward ? above : below) + "px";
    if (upward) {
      panel.style.bottom = window.innerHeight - rect.top + GAP + "px";
    } else {
      panel.style.top = rect.bottom + GAP + "px";
    }
    const filter = document.createElement("input");
    filter.placeholder = "Filter";
    const list = document.createElement("div");
    list.className = "picker-list";
    list.setAttribute("role", "listbox");
    let shown = drawOptions(select, trigger, list, "");
    filter.addEventListener("input", () => {
      shown = drawOptions(
        select,
        trigger,
        list,
        filter.value.trim().toLowerCase(),
      );
    });
    filter.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && shown.length > 0) {
        event.preventDefault();
        pick(select, trigger, shown[0].value);
      }
    });
    panel.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        closePanel();
        trigger.focus();
      }
    });
    panel.append(filter, list);
    document.body.append(panel);
    trigger.setAttribute("aria-expanded", "true");
    openPanel = { panel, trigger };
    filter.focus();
  }

  function enhancePicker(select) {
    if (select.dataset.enhanced !== undefined) {
      return;
    }
    select.dataset.enhanced = "";
    select.hidden = true;
    const trigger = document.createElement("button");
    trigger.type = "button";
    trigger.className = "picker-trigger";
    trigger.setAttribute("aria-haspopup", "listbox");
    trigger.setAttribute("aria-expanded", "false");
    select.after(trigger);
    syncPicker(select);
    trigger.addEventListener("click", () => {
      if (openPanel !== null && openPanel.trigger === trigger) {
        closePanel();
      } else {
        openPicker(select, trigger);
      }
    });
  }

  function insidePanel(target) {
    return (
      openPanel !== null &&
      target instanceof Node &&
      (openPanel.panel.contains(target) || openPanel.trigger.contains(target))
    );
  }

  // ---- switches, and a server address tested before it is stored -------------

  function slide(control) {
    const label = control.closest("label.switch");
    if (label === null || control.disabled) {
      return;
    }
    label.classList.remove("switch-inherited", "switch-on", "switch-off");
    label.classList.add(control.checked ? "switch-on" : "switch-off");
    const text = label.querySelector(".switch-label");
    if (text !== null && text.dataset.on !== undefined) {
      text.textContent = control.checked ? text.dataset.on : text.dataset.off;
    }
  }

  function untested(form) {
    const url = form.querySelector("[data-probed][name=server_url]");
    if (url === null || form.dataset.probe === undefined) {
      return false;
    }
    const typed = url.value.trim();
    return typed !== "" && typed !== url.defaultValue && !form.dataset.tested;
  }

  function untest(form) {
    if (form === null || form === undefined) {
      return;
    }
    delete form.dataset.tested;
    const said = form.querySelector("[data-probe-result]");
    if (said !== null) {
      said.hidden = true;
    }
    relabel(form);
  }

  function relabel(form) {
    const submit = form.querySelector("[type=submit]");
    if (submit !== null && form.dataset.probe !== undefined) {
      submit.textContent = untested(form) ? "Test" : "Save";
    }
  }

  async function probe(form) {
    const said = form.querySelector("[data-probe-result]");
    const field = (name) => {
      const control = form.elements.namedItem(name);
      return control === null ? "" : control.value.trim();
    };
    let message;
    try {
      const answer = await call("POST", form.dataset.probe, {
        url: field("server_url"),
        key: field("server_key"),
      });
      if (answer.ok) {
        form.dataset.tested = "1";
        message = answer.server + " answered as " + answer.model;
        if (answer.dimensions !== undefined) {
          message += ", " + answer.dimensions + " dimensions";
        }
        if (answer.fell_back === true) {
          message +=
            " - NOT the address typed. " + (answer.skipped || []).join("; ");
        }
      } else {
        message = answer.detail || "no server answered";
      }
    } catch (reason) {
      message = reason instanceof Error ? reason.message : String(reason);
    }
    if (said !== null) {
      said.textContent = message;
      said.hidden = false;
    }
    relabel(form);
  }

  // ---- the Ask page: one tool call, and exactly what came back ---------------

  const CHARS_PER_TOKEN = 4;

  function askValue(control) {
    const raw = control.value;
    const kind = control.dataset.kind;
    if (kind === "number") {
      const value = Number(raw);
      if (Number.isNaN(value)) {
        throw new Error('"' + control.name + '" must be a number');
      }
      return value;
    }
    if (kind === "boolean") {
      return raw === "true";
    }
    if (kind === "list") {
      return raw
        .split(",")
        .map((entry) => entry.trim())
        .filter((entry) => entry !== "");
    }
    if (kind === "json") {
      try {
        return JSON.parse(raw);
      } catch {
        throw new Error('"' + control.name + '" must be valid JSON');
      }
    }
    return raw;
  }

  function pretty(text) {
    try {
      return JSON.stringify(JSON.parse(text), null, 2);
    } catch {
      return text;
    }
  }

  function element(tag, className, text) {
    const made = document.createElement(tag);
    if (className) {
      made.className = className;
    }
    if (text !== undefined) {
      made.textContent = text;
    }
    return made;
  }

  function drawAnswer(into, sent, result) {
    const said = (value) => value.toLocaleString("en-US");
    const tokens = Math.ceil(result.chars / CHARS_PER_TOKEN);
    const row = element("div", "row");
    if (result.is_error) {
      row.append(element("span", "bad", "tool error"));
    }
    row.append(
      element(
        "span",
        "muted",
        said(result.ms) +
          " ms, " +
          said(result.chars) +
          " chars, about " +
          said(tokens) +
          " tokens, " +
          result.content.length +
          " block(s)",
      ),
    );
    into.replaceChildren(
      element("h2", "", "Answer"),
      row,
      element("h3", "", "Request"),
      element("pre", "source ask-block", JSON.stringify(sent, null, 2)),
    );
    result.content.forEach((block, index) => {
      const head = element("div", "row");
      const copy = element("button", "secondary copy", "Copy");
      copy.type = "button";
      copy.dataset.copy = block.text;
      const title =
        "Block " +
        (index + 1) +
        " of " +
        result.content.length +
        ", " +
        block.type;
      head.append(element("h3", "", title), copy);
      const holder = element("div");
      holder.append(
        head,
        element("pre", "source ask-block", pretty(block.text)),
      );
      into.append(holder);
    });
  }

  async function runAsk(form) {
    const submit = form.querySelector("[type=submit]");
    const errors = document.querySelector("#ask-error");
    const into = document.querySelector("#ask-answer");
    submit.disabled = true;
    submit.textContent = "Running...";
    errors.replaceChildren();
    try {
      const args = {};
      for (const control of form.elements) {
        if (control.name && control.value.trim() !== "") {
          args[control.name] = askValue(control);
        }
      }
      const sent = { name: form.dataset.tool, arguments: args };
      const result = await call("POST", "/api/ask", {
        project: form.dataset.project,
        tool: sent.name,
        arguments: sent.arguments,
      });
      drawAnswer(into, sent, result);
    } catch (reason) {
      into.replaceChildren();
      const message = reason instanceof Error ? reason.message : String(reason);
      errors.replaceChildren(errorBox(message, "the tool call"));
    }
    submit.textContent = "Run";
    gate(form);
  }

  document.addEventListener("keydown", (event) => {
    const form =
      event.target instanceof Element
        ? event.target.closest("form[data-ask]")
        : null;
    if (form !== null && event.key === "Enter" && event.ctrlKey) {
      const submit = form.querySelector("[type=submit]");
      if (!submit.disabled) {
        void runAsk(form);
      }
    }
  });

  // ---- wiring ---------------------------------------------------------------

  function wanted(modal, value) {
    // What was picked outside a modal, written into the question it asks.
    for (const slot of modal.querySelectorAll("[data-wanted]")) {
      if (slot instanceof HTMLInputElement) {
        slot.value = value;
      } else {
        slot.textContent = value || slot.dataset.empty || "";
      }
    }
    for (const part of modal.querySelectorAll("[data-when-empty]")) {
      part.hidden = value !== "";
    }
    for (const part of modal.querySelectorAll("[data-when-set]")) {
      part.hidden = value === "";
    }
  }

  function localTimes(root) {
    for (const time of root.querySelectorAll("time[data-local]")) {
      const moment = new Date(time.dateTime);
      if (!Number.isNaN(moment.getTime())) {
        time.textContent =
          time.dataset.local === "time"
            ? moment.toLocaleTimeString()
            : moment.toLocaleString("en-GB");
      }
    }
  }

  function setUp(root) {
    for (const select of root.querySelectorAll("select[data-picker]")) {
      enhancePicker(select);
    }
    for (const form of root.querySelectorAll("form")) {
      if (form.dataset.draft) {
        restore(form);
      }
      if (
        form.dataset.draft !== undefined ||
        form.dataset.dirty !== undefined
      ) {
        mark(form);
      }
      for (const control of form.elements) {
        mirror(control);
        if (control.type === "checkbox" && control.dataset.touched) {
          slide(control);
        }
      }
      relabel(form);
      gate(form);
    }
    localTimes(root);
    for (const focused of root.querySelectorAll("[data-open-now]")) {
      focused.hidden = false;
    }
  }

  document.addEventListener("click", (event) => {
    const target = event.target instanceof Element ? event.target : null;
    if (target === null) {
      return;
    }
    const copy = target.closest("[data-copy]");
    if (copy !== null) {
      navigator.clipboard.writeText(copy.dataset.copy).then(
        () => (copy.textContent = "Copied"),
        () => (copy.textContent = "Copy"),
      );
      return;
    }
    const opener = target.closest("[data-open]");
    if (opener !== null) {
      const modal = document.querySelector(opener.dataset.open);
      const source = opener.dataset.fill
        ? document.querySelector(opener.dataset.fill)
        : null;
      if (source !== null && source.value === "") {
        return;
      }
      if (modal !== null) {
        if (source !== null) {
          wanted(modal, source.value);
        }
        modal.hidden = false;
        const first = modal.querySelector("[autofocus]");
        if (first !== null) {
          first.focus();
        }
      }
      return;
    }
    const toggler = target.closest("[data-toggle]");
    if (toggler !== null) {
      const shown = document.querySelector(toggler.dataset.toggle);
      if (shown !== null) {
        shown.hidden = !shown.hidden;
        const other = toggler.dataset.alt;
        toggler.dataset.alt = toggler.textContent;
        toggler.textContent = other;
      }
      return;
    }
    const closer = target.closest("[data-close]");
    if (closer !== null) {
      const modal = closer.closest(".modal-backdrop");
      if (modal !== null) {
        modal.hidden = true;
        const revert = modal.dataset.revert;
        if (revert) {
          const select = document.querySelector(revert);
          if (select !== null) {
            select.value = storedOf(select);
          }
        }
      }
      return;
    }
    const discarder = target.closest("[data-discard]");
    if (discarder !== null && discarder.form !== null) {
      discard(discarder.form);
      return;
    }
    const link = target.closest("button[data-href]");
    if (link !== null) {
      window.location.href = link.dataset.href;
      return;
    }
    const actor = target.closest("button[data-api]");
    if (actor !== null) {
      event.preventDefault();
      void act(actor);
    }
  });

  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (form instanceof HTMLFormElement && form.dataset.ask !== undefined) {
      event.preventDefault();
      void runAsk(form);
      return;
    }
    if (form instanceof HTMLFormElement && form.dataset.search !== undefined) {
      event.preventDefault();
      return;
    }
    if (form instanceof HTMLFormElement && form.dataset.api !== undefined) {
      event.preventDefault();
      if (untested(form)) {
        void probe(form);
      } else {
        void act(form);
      }
    }
  });

  function changed(event) {
    const control = event.target;
    if (!(control instanceof Element) || control.form === undefined) {
      return;
    }
    if (
      control.type === "file" &&
      control.dataset.into &&
      event.type === "change"
    ) {
      const into = document.querySelector(control.dataset.into);
      const file = control.files === null ? undefined : control.files[0];
      if (into !== null && file !== undefined) {
        void file.text().then((read) => {
          into.value = read;
          into.dispatchEvent(new Event("input", { bubbles: true }));
        });
      }
      return;
    }
    if (
      control.type === "checkbox" &&
      control.dataset.inherited !== undefined
    ) {
      control.dataset.touched = "1";
    }
    if (control.type === "checkbox") {
      slide(control);
    }
    if (control.dataset.probed !== undefined) {
      untest(control.form);
    }
    mirror(control);
    const form = control.form;
    if (form === null) {
      if (event.type === "change" && control.dataset.go) {
        window.location.href = control.dataset.go.replace(
          "{value}",
          encodeURIComponent(control.value),
        );
      } else if (event.type === "change" && control.dataset.asks) {
        ask(control);
      }
      return;
    }
    if (form.dataset.draft !== undefined || form.dataset.dirty !== undefined) {
      mark(form);
    }
    gate(form);
    if (event.type === "change" && control.dataset.go) {
      window.location.href = control.dataset.go.replace(
        "{value}",
        encodeURIComponent(control.value),
      );
      return;
    }
    if (event.type === "change" && form.dataset.submitOnChange !== undefined) {
      void act(form);
      return;
    }
    if (event.type === "change" && control.dataset.asks) {
      ask(control);
    }
  }

  // A select that asks before it writes: it opens the question it names.
  function ask(control) {
    const modal = document.querySelector(control.dataset.asks);
    if (modal !== null && control.value !== storedOf(control)) {
      wanted(modal, control.value);
      modal.hidden = false;
    }
  }

  document.addEventListener("input", changed);
  document.addEventListener("change", changed);

  document.addEventListener("mousedown", (event) => {
    if (!insidePanel(event.target)) {
      closePanel();
    }
  });
  window.addEventListener(
    "scroll",
    (event) => {
      if (!insidePanel(event.target)) {
        closePanel();
      }
    },
    true,
  );
  window.addEventListener("resize", closePanel);

  document.addEventListener("DOMContentLoaded", () => setUp(document));
  document.addEventListener("htmx:afterSwap", (event) => {
    closePanel();
    setUp(event.target instanceof Element ? event.target : document);
    if (event.target instanceof Element && event.target.id === "main") {
      setUp(document);
    }
  });

  window.enggraph = { call, refresh, showError, clearError, errorBox };
})();
