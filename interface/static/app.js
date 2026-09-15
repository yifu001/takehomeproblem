/* Fraud Ops Assistant — single-page chat UI (presentation only).
 *
 * This module renders what the server resolves and returns; every access decision,
 * identity resolution, and scope enforcement lives in the FastAPI backend and the
 * policy engine. Nothing here inspects SQL or warehouse data, and nothing here can
 * grant access: the acting identity is chosen from the server's own /users list and
 * the server answers under that identity's enforced scope.
 *
 * State is in-memory per page load (deliberately never persisted): a reload always
 * returns to the identity picker with an empty thread. Conversations are keyed by
 * identity, so switching identity parks the previous conversation and it resumes only
 * when that same identity is picked again; the server additionally refuses (409) any
 * conversation posted under a different identity, so cross-identity content cannot
 * appear in either direction.
 *
 * Assistant turns render as one of four visually distinct states, driven by the
 * response's answer_kind:
 *   answer  — prose, plus an inline Vega-Lite chart when the turn produced one
 *             (rows of an authorized result handle), plus a neutral "No matching
 *             rows" chip when every executed query returned zero rows;
 *   clarify — an interactive prompt card inviting an answer in the same thread;
 *   decline — a red-bordered access-denied card carrying the audit record's refusal
 *             category (never worded as "no results");
 *   error   — a distinct error card for agent failures, network failures (one
 *             automatic retry is made first: the first POST after a service restart
 *             can land on the browser's dead pooled keep-alive connection while the
 *             service is healthy), or a conversation id left stale by a service
 *             restart — never a silent empty answer.
 * Each assistant message carries a collapsed-by-default transparency panel with the
 * turn's tool calls, the post-rewrite SQL that actually executed, and the audit
 * record's scope details. Raw model transcripts, token counts, and costs are
 * deliberately not displayed anywhere.
 */

(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);

  const els = {
    pickerOverlay: $("picker-overlay"),
    identityGrid: $("identity-grid"),
    pickerError: $("picker-error"),
    pickerRetry: $("picker-retry-btn"),
    identityHeader: $("identity-header"),
    identityName: $("identity-name"),
    identityRoleBadge: $("identity-role-badge"),
    identityRegion: $("identity-region"),
    identityScope: $("identity-scope"),
    switchBtn: $("switch-identity-btn"),
    thread: $("thread"),
    blockedHint: $("blocked-hint"),
    composerForm: $("composer-form"),
    questionInput: $("question-input"),
    sendBtn: $("send-btn"),
  };

  // Per-page-load session state. Nothing persists beyond the page.
  const state = {
    users: [],
    current: null,             // the chosen identity (from GET /users)
    conversations: new Map(),  // user_id -> conversation_id issued by the server
    threads: new Map(),        // user_id -> array of message data objects
    pending: false,
  };

  const MAX_TOOL_INPUT_CHARS = 400;

  // ------------------------------------------------------------------ helpers

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function regionLabel(user) {
    return user.region || "(all regions)";
  }

  function scrollThreadToEnd() {
    els.thread.scrollTop = els.thread.scrollHeight;
  }

  function threadDataFor(userId) {
    if (!state.threads.has(userId)) state.threads.set(userId, []);
    return state.threads.get(userId);
  }

  function compactJson(value) {
    try {
      const text = JSON.stringify(value);
      return text.length > MAX_TOOL_INPUT_CHARS ? `${text.slice(0, MAX_TOOL_INPUT_CHARS)}…` : text;
    } catch (err) {
      return "";
    }
  }

  // ----------------------------------------------------------- identity picker

  async function loadUsers() {
    els.pickerError.classList.add("hidden");
    els.pickerRetry.classList.add("hidden");
    els.identityGrid.replaceChildren();
    els.identityGrid.appendChild(el("p", "picker-loading", "Loading identities…"));
    try {
      const resp = await fetch("/users");
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const users = await resp.json();
      if (!Array.isArray(users) || users.length === 0) throw new Error("empty identity list");
      state.users = users;
      renderPicker(users);
    } catch (err) {
      els.identityGrid.replaceChildren();
      els.pickerError.textContent = "Could not load the identity list. Is the service running?";
      els.pickerError.classList.remove("hidden");
      els.pickerRetry.classList.remove("hidden");
    }
  }

  function renderPicker(users) {
    els.identityGrid.replaceChildren();
    for (const user of users) {
      const card = el("button", "identity-card");
      card.type = "button";
      card.addEventListener("click", () => chooseIdentity(user));
      card.append(
        el("span", "identity-card-name", user.full_name),
        el("span", `badge badge-${user.role}`, user.role),
        el("span", "identity-card-region", regionLabel(user)),
        el("span", "identity-card-id", user.user_id),
      );
      els.identityGrid.appendChild(card);
    }
  }

  async function chooseIdentity(user) {
    state.current = user;
    els.pickerOverlay.classList.add("hidden");
    els.blockedHint.classList.add("hidden");
    els.identityHeader.classList.remove("hidden");
    renderIdentityHeader(user);
    els.identityScope.textContent = "Access scope: resolving…";
    loadScope(user.user_id);
    renderThread(user.user_id);
    setComposer(true);
    els.questionInput.focus();
  }

  function renderIdentityHeader(user) {
    els.identityName.textContent = user.full_name;
    els.identityRoleBadge.textContent = user.role;
    els.identityRoleBadge.className = `badge badge-${user.role}`;
    els.identityRegion.textContent = regionLabel(user);
  }

  // The scope line comes from the server's own resolution (GET /scope/{user_id} →
  // audit.resolved_scope), never from a client-side guess, so what the header shows
  // is exactly what the audit trail records for the turn.
  async function loadScope(userId) {
    try {
      const resp = await fetch(`/scope/${encodeURIComponent(userId)}`);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      renderScopeLine(await resp.json());
    } catch (err) {
      els.identityScope.textContent =
        "Access scope: resolved and enforced by the server on every turn";
    }
  }

  function renderScopeLine(scope) {
    if (!scope) return;
    const parts = [
      `rows: ${scope.row_scope}`,
      `columns: ${scope.column_tiers && scope.column_tiers.length ? scope.column_tiers.join(", ") : "none on customer data"}`,
      `case_notes: ${scope.case_notes ? "allowed" : "denied"}`,
    ];
    if (scope.aggregate_only) parts.push("aggregate-only queries");
    els.identityScope.textContent = `Access scope — ${parts.join(" · ")}`;
  }

  function switchIdentity() {
    state.current = null;
    els.identityHeader.classList.add("hidden");
    els.identityScope.textContent = "";
    els.thread.replaceChildren();
    els.questionInput.value = "";
    els.blockedHint.classList.add("hidden");
    setComposer(false);
    els.pickerOverlay.classList.remove("hidden");
    // Conversations and threads stay parked per identity in memory: picking an
    // identity again resumes only that identity's own conversation (server-enforced).
  }

  // ---------------------------------------------------------------- composer

  function setComposer(enabled) {
    const on = enabled && !state.pending;
    els.questionInput.disabled = !on;
    els.sendBtn.disabled = !on;
    els.sendBtn.textContent = state.pending ? "Thinking…" : "Ask";
  }

  els.composerForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const question = els.questionInput.value.trim();
    if (state.pending) return;
    if (!state.current) {
      // No identity, no request: never a /chat call with a placeholder identity.
      els.blockedHint.classList.remove("hidden");
      return;
    }
    if (!question) return;
    els.questionInput.value = "";
    ask(question);
  });

  els.switchBtn.addEventListener("click", switchIdentity);
  els.pickerRetry.addEventListener("click", loadUsers);

  // ------------------------------------------------------------- ask a turn

  // One automatic retry on a network-level failure (fetch rejected before any HTTP
  // answer): Chrome can hold a dead pooled keep-alive connection right after a service
  // restart while the service itself is healthy. HTTP error statuses are real answers
  // and are never retried; if the retry also fails, the fetch throws and the normal
  // error card renders below.
  async function postChat(payload) {
    try {
      return await fetch("/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
    } catch (networkError) {
      return await fetch("/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
    }
  }

  async function ask(question) {
    const user = state.current;
    if (!user || state.pending) return;
    const userId = user.user_id;
    state.pending = true;
    setComposer(true);
    els.switchBtn.disabled = true;
    for (const node of els.thread.querySelectorAll(".clarify-reply-input, .clarify-reply-btn")) {
      node.disabled = true;
    }
    appendUserMessage(userId, question);
    const placeholder = el("p", "pending-note", "Working on it…");
    els.thread.appendChild(placeholder);
    scrollThreadToEnd();
    try {
      const resp = await postChat({
        user_id: userId,
        question,
        conversation_id: state.conversations.get(userId) || null,
      });
      if (!resp.ok) {
        await handleHttpError(resp, userId);
        return;
      }
      const data = await resp.json();
      state.conversations.set(userId, data.conversation_id);
      if (data.audit && data.audit.resolved_scope) renderScopeLine(data.audit.resolved_scope);
      appendAssistantMessage(userId, data);
    } catch (err) {
      appendErrorCard(
        userId,
        "Could not reach the assistant service. If it just restarted, your next question will start a fresh conversation.",
      );
    } finally {
      state.pending = false;
      placeholder.remove();
      els.switchBtn.disabled = false;
      for (const node of els.thread.querySelectorAll(".clarify-reply-input, .clarify-reply-btn")) {
        node.disabled = false;
      }
      setComposer(true);
      scrollThreadToEnd();
    }
  }

  async function handleHttpError(resp, userId) {
    let code = "internal_error";
    let message = "The service returned an unexpected response.";
    try {
      const body = await resp.json();
      if (body && body.error) {
        code = body.error.code || code;
        message = body.error.message || message;
      }
    } catch (err) {
      // Non-JSON body: keep the generic text (never echo anything unstructured).
    }
    if (code === "unknown_conversation") {
      // The conversation id is stale (the service restarted and dropped its state).
      // Recover cleanly: drop it and start the next turn as a fresh conversation.
      state.conversations.delete(userId);
      state.threads.set(userId, []);
      els.thread.replaceChildren();
      appendErrorCard(
        userId,
        "This conversation is no longer available — the service may have restarted. " +
        "A new conversation will start with your next question.",
        { recovery: true },
      );
      return;
    }
    appendErrorCard(userId, `The request could not be completed (${code}). ${message}`);
  }

  // ---------------------------------------------------------- thread rendering

  function appendUserMessage(userId, text) {
    const msg = { kind: "user", text };
    threadDataFor(userId).push(msg);
    els.thread.appendChild(renderMessage(msg));
    scrollThreadToEnd();
  }

  function appendAssistantMessage(userId, data) {
    const msg = assistantDataFromResponse(data);
    threadDataFor(userId).push(msg);
    els.thread.appendChild(renderMessage(msg));
    scrollThreadToEnd();
  }

  function appendErrorCard(userId, text, options) {
    const msg = { kind: "error", text, recovery: !!(options && options.recovery) };
    threadDataFor(userId).push(msg);
    els.thread.appendChild(renderMessage(msg));
    scrollThreadToEnd();
  }

  function renderThread(userId) {
    els.thread.replaceChildren();
    for (const msg of threadDataFor(userId)) els.thread.appendChild(renderMessage(msg));
    scrollThreadToEnd();
  }

  function renderMessage(msg) {
    if (msg.kind === "user") return renderUserMessage(msg.text);
    if (msg.kind === "error") {
      const wrap = el("article", "msg error-msg");
      wrap.appendChild(renderErrorCard(msg.text, { recovery: msg.recovery }));
      return wrap;
    }
    return renderAssistantMessage(msg);
  }

  function renderUserMessage(text) {
    const wrap = el("article", "msg user");
    wrap.appendChild(el("p", "msg-text", text));
    return wrap;
  }

  function assistantDataFromResponse(data) {
    const auditRec = data.audit || {};
    const msg = {
      kind: "assistant",
      answer: data.answer || "",
      answerKind: data.answer_kind || "answer",
      chart: data.chart || null,
      audit: auditRec,
      emptyResult: false,
    };
    if (msg.answerKind === "answer" && !msg.chart && isEmptyResult(auditRec)) {
      msg.emptyResult = true;
    }
    return msg;
  }

  // In-scope but genuinely empty: every executed query returned zero rows and nothing
  // was refused. A COUNT-style aggregate returns one row even when the count is 0, so
  // it never reads as an empty result set.
  function isEmptyResult(auditRec) {
    const calls = (auditRec.tool_calls || []).filter((call) => call.tool === "run_sql");
    if (!calls.length) return false;
    if (calls.some((call) => call.refusal)) return false;
    return calls.every((call) => (call.rows_returned || 0) === 0);
  }

  function renderAssistantMessage(msg) {
    const wrap = el("article", "msg assistant");
    if (msg.answerKind === "decline") {
      wrap.appendChild(renderRefusalCard(msg));
    } else if (msg.answerKind === "clarify") {
      wrap.appendChild(renderClarifyCard(msg));
    } else if (msg.answerKind === "error") {
      wrap.appendChild(renderErrorCard(
        "The assistant hit a problem and could not complete this turn. Nothing was answered — please try again.",
      ));
    } else {
      wrap.appendChild(renderAnswerBody(msg));
    }
    wrap.appendChild(renderTransparency(msg));
    return wrap;
  }

  function renderAnswerBody(msg) {
    const body = el("div", "answer-body");
    body.appendChild(el("p", "answer-text", msg.answer));
    if (msg.chart) body.appendChild(renderChartArea(msg.chart));
    if (msg.emptyResult) body.appendChild(renderEmptyState());
    return body;
  }

  function renderEmptyState() {
    const chip = el("div", "empty-state");
    chip.setAttribute("role", "status");
    chip.append(
      el("span", "empty-state-title", "No matching rows"),
      el("span", "empty-state-detail", "The query ran in your scope and returned zero rows."),
    );
    return chip;
  }

  // ------------------------------------------------------------------- charts

  function renderChartArea(spec) {
    const area = el("div", "chart-area");
    const values = spec && spec.data && Array.isArray(spec.data.values) ? spec.data.values : [];
    if (values.length === 0) {
      const empty = el("div", "empty-chart", "No data to chart — the authorized result set is empty.");
      empty.setAttribute("role", "status");
      area.appendChild(empty);
      return area;
    }
    const host = el("div", "chart-container");
    area.appendChild(host);
    area.appendChild(renderChartDataTable(spec));
    embedChart(host, spec);
    return area;
  }

  // The authorized rows the chart is drawn from, as a small table next to it — the
  // rendered marks and these numbers come from the same policy-issued result handle.
  function renderChartDataTable(spec) {
    const values = (spec.data && spec.data.values) || [];
    const wrap = el("details", "chart-data");
    wrap.appendChild(el("summary", null, "Chart data"));
    const table = el("table", "chart-data-table");
    const keys = Object.keys(values[0] || {});
    const head = el("thead");
    const headRow = el("tr");
    for (const key of keys) headRow.appendChild(el("th", null, key));
    head.appendChild(headRow);
    table.appendChild(head);
    const tbody = el("tbody");
    for (const row of values) {
      const tr = el("tr");
      for (const key of keys) {
        tr.appendChild(el("td", null, String(row[key] === null || row[key] === undefined ? "" : row[key])));
      }
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    wrap.appendChild(table);
    return wrap;
  }

  async function embedChart(host, spec) {
    if (typeof window.vegaEmbed !== "function") {
      renderTableFallback(host, spec);
      return;
    }
    try {
      // SVG renderer so the chart's labels and marks are real DOM nodes (readable,
      // screenshot-free, and comparable against the answer's numbers).
      await window.vegaEmbed(host, spec, { actions: false, renderer: "svg" });
      host.classList.add("chart-ready");
    } catch (err) {
      host.replaceChildren();
      renderTableFallback(host, spec);
    }
  }

  function renderTableFallback(host, spec) {
    // Dependency-free fallback so the authorized data is still visible if the
    // vendored chart runtime ever fails to load.
    const values = spec && spec.data && Array.isArray(spec.data.values) ? spec.data.values : [];
    const table = el("table", "chart-fallback-table");
    if (values.length) {
      const keys = Object.keys(values[0]);
      const head = el("thead");
      const headRow = el("tr");
      for (const key of keys) headRow.appendChild(el("th", null, key));
      head.appendChild(headRow);
      table.appendChild(head);
      const tbody = el("tbody");
      for (const row of values.slice(0, 10)) {
        const tr = el("tr");
        for (const key of keys) tr.appendChild(el("td", null, String(row[key] === null || row[key] === undefined ? "" : row[key])));
        tbody.appendChild(tr);
      }
      table.appendChild(tbody);
    }
    host.append(el("p", "chart-fallback-note", "Chart data (the chart runtime could not render it):"), table);
  }

  // -------------------------------------------------------- the three states

  function refusalCategory(auditRec) {
    for (const call of auditRec.tool_calls || []) {
      if (call && call.refusal && call.refusal.category) return call.refusal.category;
    }
    return null;
  }

  function renderRefusalCard(msg) {
    const card = el("div", "refusal-card");
    card.setAttribute("role", "alert");
    card.appendChild(el("p", "refusal-title", "Access denied"));
    const category = refusalCategory(msg.audit);
    if (category) card.appendChild(el("span", "refusal-category", category));
    if (msg.answer) card.appendChild(el("p", "refusal-reason", msg.answer));
    return card;
  }

  function renderClarifyCard(msg) {
    const card = el("div", "clarify-card");
    card.appendChild(el("p", "clarify-title", "One more detail needed"));
    card.appendChild(el("p", "clarify-question", msg.answer || "Could you clarify?"));
    const form = el("form", "clarify-reply-form");
    const input = el("input", "clarify-reply-input");
    input.type = "text";
    input.placeholder = "Type your answer…";
    input.autocomplete = "off";
    input.setAttribute("aria-label", "Reply to the clarifying question");
    const btn = el("button", "clarify-reply-btn", "Reply");
    btn.type = "submit";
    form.append(input, btn);
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      const text = input.value.trim();
      if (!text || state.pending || !state.current) return;
      input.value = "";
      ask(text);
    });
    card.appendChild(form);
    card.appendChild(el("p", "clarify-note", "Your reply continues this conversation."));
    return card;
  }

  function renderErrorCard(text, options) {
    const card = el("div", "error-card");
    card.setAttribute("role", "alert");
    card.appendChild(el("p", "error-title", "Something went wrong"));
    card.appendChild(el("p", "error-text", text));
    if (options && options.recovery) {
      const btn = el("button", "error-recovery-btn", "Start a new conversation");
      btn.type = "button";
      btn.addEventListener("click", () => {
        if (state.current) {
          state.threads.set(state.current.user_id, []);
        }
        els.thread.replaceChildren();
        els.questionInput.focus();
      });
      card.appendChild(btn);
    }
    return card;
  }

  // ------------------------------------------------------ transparency panel

  function renderTransparency(msg) {
    const details = el("details", "transparency");
    details.appendChild(el("summary", "transparency-summary", "Details: tool calls & executed SQL"));
    const body = el("div", "transparency-body");

    const calls = (msg.audit && msg.audit.tool_calls) || [];
    const callsSection = el("section", "transparency-section");
    callsSection.appendChild(el("h4", null, "Tool calls"));
    if (calls.length) {
      const list = el("ul", "tool-call-list");
      for (const call of calls) list.appendChild(renderToolCall(call));
      callsSection.appendChild(list);
    } else {
      callsSection.appendChild(el("p", "transparency-empty", "No tool calls this turn."));
    }
    body.appendChild(callsSection);
    body.appendChild(renderAuditSummary(msg.audit));

    details.appendChild(body);
    return details;
  }

  function renderToolCall(call) {
    const item = el("li", "tool-call");
    item.appendChild(el("span", "tool-name", call.tool || "unknown"));
    if (call.sql_requested) item.appendChild(sqlLine("requested", call.sql_requested));
    if (call.sql_executed) item.appendChild(sqlLine("executed (post-rewrite)", call.sql_executed));
    if (call.rewrites_applied && call.rewrites_applied.length) {
      item.appendChild(el("p", "tool-meta", `rewrites: ${call.rewrites_applied.join(", ")}`));
    }
    if (call.refusal) {
      item.appendChild(el("p", "tool-refusal",
        `refused: ${call.refusal.category}${call.refusal.detail ? ` — ${call.refusal.detail}` : ""}`));
    }
    if (call.tool === "run_sql" && typeof call.rows_returned === "number") {
      item.appendChild(el("p", "tool-meta", `rows returned: ${call.rows_returned}`));
    }
    if (call.args && Object.keys(call.args).length) {
      item.appendChild(el("p", "tool-meta", `arguments: ${compactJson(call.args)}`));
    }
    if (call.reason && (call.tool === "decline" || call.tool === "ask_clarifying_question")) {
      item.appendChild(el("p", "tool-meta", `${call.tool === "decline" ? "stated reason" : "asked"}: ${call.reason}`));
    }
    return item;
  }

  function sqlLine(label, sql) {
    const line = el("div", "sql-line");
    line.appendChild(el("span", "sql-label", label));
    line.appendChild(el("code", "sql-code", sql));
    return line;
  }

  function renderAuditSummary(auditRec) {
    const section = el("section", "transparency-section");
    section.appendChild(el("h4", null, "Audit record"));
    const grid = el("dl", "audit-grid");
    const row = (term, def) => {
      grid.appendChild(el("dt", null, term));
      grid.appendChild(el("dd", null, def));
    };
    if (auditRec && auditRec.turn_id) row("turn", auditRec.turn_id);
    const identity = (auditRec && auditRec.identity) || {};
    if (identity.user_id) {
      row("identity", `${identity.user_id} (${identity.role}${identity.region ? `, ${identity.region}` : ""})`);
    }
    const scope = (auditRec && auditRec.resolved_scope) || null;
    if (scope) {
      row("column tiers", scope.column_tiers && scope.column_tiers.length ? scope.column_tiers.join(", ") : "none on customer data");
      row("row scope", scope.row_scope);
      row("case_notes", scope.case_notes ? "allowed" : "denied");
      row("aggregate only", scope.aggregate_only ? "yes" : "no");
      row("tables denied", scope.tables_denied && scope.tables_denied.length ? scope.tables_denied.join(", ") : "none");
    }
    const redactions = (auditRec && auditRec.redactions_applied) || [];
    row("redactions", redactions.length ? redactions.join(", ") : "none");
    section.appendChild(grid);
    return section;
  }

  // --------------------------------------------------------------------- init

  setComposer(false);
  loadUsers();
})();
