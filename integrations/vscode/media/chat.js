// The chat view's page script. Renders the transcript the extension posts and sends the
// person's actions back. Every piece of text is set with textContent, never parsed as HTML.
(function () {
  const vscode = acquireVsCodeApi();
  const log = document.getElementById("log");
  const status = document.getElementById("status");
  const form = document.getElementById("composer");
  const input = document.getElementById("input");
  const stopButton = document.getElementById("stop");
  const sendButton = document.getElementById("send");
  let rendered = "";

  const saved = vscode.getState();
  if (saved && typeof saved.draft === "string") input.value = saved.draft;

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  // A small, safe Markdown subset: fenced code blocks, inline code, bold, paragraphs.
  function markdown(text) {
    const box = el("div", "md");
    const parts = text.split(/```/);
    parts.forEach((part, i) => {
      if (i % 2 === 1) {
        const firstBreak = part.indexOf("\n");
        const lang = firstBreak > -1 ? part.slice(0, firstBreak).trim() : "";
        const code = firstBreak > -1 && /^[\w+#.-]*$/.test(lang) ? part.slice(firstBreak + 1) : part;
        const pre = el("pre");
        pre.appendChild(el("code", "", code.replace(/\n$/, "")));
        box.appendChild(pre);
        return;
      }
      for (const para of part.split(/\n{2,}/)) {
        if (!para.trim()) continue;
        const p = el("p");
        para.split(/(`[^`\n]+`|\*\*[^*\n]+\*\*)/).forEach((bit) => {
          if (bit.startsWith("`") && bit.endsWith("`") && bit.length > 2) p.appendChild(el("code", "", bit.slice(1, -1)));
          else if (bit.startsWith("**") && bit.endsWith("**") && bit.length > 4) p.appendChild(el("strong", "", bit.slice(2, -2)));
          else if (bit) {
            bit.split("\n").forEach((line, j) => {
              if (j > 0) p.appendChild(document.createElement("br"));
              p.appendChild(document.createTextNode(line));
            });
          }
        });
        box.appendChild(p);
      }
    });
    return box;
  }

  const TOOL_MARK = { in_progress: "○", pending: "○", completed: "✓", failed: "✗" };
  const ANSWER_TEXT = { allow_once: "Allowed once", allow_always: "Allowed for this conversation",
    reject_once: "Refused", reject_always: "Refused", dismissed: "Not answered, so refused" };

  function entry(e) {
    switch (e.kind) {
      case "user":
        return el("div", "msg user", e.text);
      case "agent": {
        const box = el("div", "msg agent");
        box.appendChild(markdown(e.text));
        return box;
      }
      case "tool": {
        const row = el("div", "tool " + e.status);
        row.appendChild(el("span", "mark", TOOL_MARK[e.status] || "○"));
        row.appendChild(el("span", "title", e.title));
        return row;
      }
      case "notice":
        return el("div", "notice " + e.level, e.text);
      case "permission": {
        const box = el("div", "permission" + (e.answer ? " answered" : ""));
        box.appendChild(el("div", "question", "The agent asks: " + e.title));
        if (e.answer) {
          box.appendChild(el("div", "answer", ANSWER_TEXT[e.answer] || e.answer));
        } else {
          const row = el("div", "choices");
          for (const option of e.options) {
            const b = el("button", option.kind.startsWith("reject") ? "secondary" : "", option.name);
            b.type = "button";
            b.addEventListener("click", () => vscode.postMessage({ type: "permission", id: e.id, optionId: option.optionId }));
            row.appendChild(b);
          }
          box.appendChild(row);
        }
        return box;
      }
      default:
        return el("div");
    }
  }

  function render(state) {
    const key = JSON.stringify(state);
    if (key === rendered) return;
    rendered = key;
    const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
    log.replaceChildren();
    if (!state.entries.length) {
      const empty = el("div", "empty");
      empty.appendChild(el("p", "", "Ask the agent to explain, change or fix something in this workspace. It asks before it edits a file or runs a command."));
      const link = el("button", "link", "Open the ABP dashboard");
      link.type = "button";
      link.addEventListener("click", () => vscode.postMessage({ type: "openDashboard" }));
      empty.appendChild(link);
      log.appendChild(empty);
    }
    for (const e of state.entries) log.appendChild(entry(e));
    const words = { stopped: "Not started", starting: "Starting…", idle: "Ready", working: "Working…" };
    status.textContent = (words[state.state] || state.state) + (state.model ? " · " + state.model : "");
    status.className = state.state;
    const working = state.state === "working" || state.state === "starting";
    stopButton.hidden = !working;
    sendButton.disabled = working;
    if (atBottom) log.scrollTop = log.scrollHeight;
  }

  function send() {
    const text = input.value.trim();
    if (!text || sendButton.disabled) return;
    vscode.postMessage({ type: "send", text });
    input.value = "";
    vscode.setState({ draft: "" });
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    send();
  });
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      send();
    }
  });
  input.addEventListener("input", () => vscode.setState({ draft: input.value }));
  stopButton.addEventListener("click", () => vscode.postMessage({ type: "cancel" }));
  window.addEventListener("message", (event) => {
    if (event.data && event.data.type === "state") render(event.data);
  });
  vscode.postMessage({ type: "ready" });
})();
