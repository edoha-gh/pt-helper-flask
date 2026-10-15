(() => {
  "use strict";

  const boot = JSON.parse(document.getElementById("boot").textContent);
  const state = {
    q: boot.query || "",
    phase: boot.phase || "",
    phases: boot.phases,
    results: boot.results,
    total: boot.total,
  };

  const input = document.getElementById("q");
  const list = document.getElementById("results");
  const heading = document.getElementById("results-title");
  const nav = document.getElementById("phase-nav");
  const targetInput = document.getElementById("target");

  const NOISE = {
    quiet: "Quiet",
    noticeable: "Noticeable",
    loud: "Loud",
  };

  // ---------- helpers ----------

  const store = {
    get(key) { try { return localStorage.getItem(key) || ""; } catch { return ""; } },
    set(key, value) { try { localStorage.setItem(key, value); } catch { /* storage unavailable */ } },
  };

  let target = store.get("pt-target");

  const el = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };

  const queryTokens = () => [...new Set(state.q.toLowerCase().match(/[a-z0-9]+/g) || [])];

  // Wrap matching words in <mark>. Text goes in as text nodes, so nothing is parsed as HTML.
  function highlight(text) {
    const tokens = queryTokens();
    const frag = document.createDocumentFragment();
    if (!tokens.length) { frag.append(text); return frag; }
    const pattern = tokens.map(t => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|");
    text.split(new RegExp(`(${pattern})`, "gi")).forEach((part, i) => {
      if (!part) return;
      if (i % 2) frag.append(el("mark", "", part));
      else frag.append(part);
    });
    return frag;
  }

  const withTarget = cmd => (target ? cmd.split("$TARGET").join(target) : cmd);

  // ---------- rendering ----------

  function renderPhases() {
    nav.replaceChildren();
    const total = state.phases.reduce((n, p) => n + p.count, 0);
    const items = [{ id: "", name: "All phases", count: total }, ...state.phases];

    items.forEach(p => {
      const li = el("li");
      const btn = el("button", "phase-btn");
      btn.type = "button";
      btn.dataset.phase = p.id;
      btn.setAttribute("aria-pressed", String(state.phase === p.id));
      if (p.count === 0) btn.classList.add("is-empty");
      btn.append(el("span", "", p.name), el("span", "phase-count", String(p.count)));
      li.append(btn);
      nav.append(li);
    });
  }

  function renderEntry(entry) {
    const article = el("article", "entry");

    const head = el("div", "entry-head");
    const title = el("h3");
    title.append(highlight(entry.title));
    head.append(title);
    if (NOISE[entry.noise]) {
      const noise = el("span", `noise noise-${entry.noise}`, NOISE[entry.noise]);
      noise.title = "How likely this step is to show up in logs and alerts";
      head.append(noise);
    }
    article.append(head);

    const meta = el("div", "entry-meta");
    meta.append(el("span", "phase-tag", entry.phase_name));
    (entry.tags || []).forEach(t => {
      const tag = el("span", "tag");
      tag.append(highlight(t));
      meta.append(tag);
    });
    article.append(meta);

    if (entry.summary) {
      const p = el("p", "summary");
      p.append(highlight(entry.summary));
      article.append(p);
    }

    if (entry.commands && entry.commands.length) {
      const cmds = el("div", "commands");
      entry.commands.forEach(c => {
        const block = el("div", "cmd");
        const id = `cmd-${entry.id}-${cmds.children.length}`;
        const label = el("span", "cmd-label", c.label || "Command");
        label.id = `${id}-label`;

        const box = el("div", "cmd-box");
        const pre = el("pre");
        const code = el("code", "", withTarget(c.cmd));
        code.dataset.raw = c.cmd;
        pre.append(code);

        const btn = el("button", "copy", "Copy");
        btn.type = "button";
        btn.setAttribute("aria-labelledby", `${id}-label`);
        btn.setAttribute("aria-describedby", `${id}-label`);

        box.append(pre, btn);
        block.append(label, box);
        cmds.append(block);
      });
      article.append(cmds);
    }

    if (entry.caution) {
      const c = el("p", "caution");
      c.append(el("strong", "", "Caution: "), entry.caution);
      article.append(c);
    }
    if (entry.notes) {
      const n = el("p", "notes");
      n.append(highlight(entry.notes));
      article.append(n);
    }
    return article;
  }

  function renderEmpty() {
    const box = el("div", "empty");
    box.append(
      el("h3", "", `No steps match "${state.q}"`),
      el("p", "", "Try a service name such as smb or ldap, a port number such as 445, or a tool such as nmap. Fewer words usually work better."),
    );
    const btn = el("button", "", "Clear search");
    btn.type = "button";
    btn.dataset.action = "clear";
    box.append(btn);
    return box;
  }

  function renderResults() {
    const phase = state.phases.find(p => p.id === state.phase);
    const where = phase ? ` in ${phase.name}` : "";
    const n = state.results.length;
    const noun = n === 1 ? "step" : "steps";

    heading.textContent = state.q
      ? `${n} ${noun} for "${state.q}"${where}`
      : `${n} ${noun}${where || " across all phases"}`;

    list.replaceChildren();
    if (n === 0) { list.append(renderEmpty()); return; }
    state.results.forEach(entry => list.append(renderEntry(entry)));
  }

  function render() {
    renderPhases();
    renderResults();
  }

  // ---------- data ----------

  let timer = null;
  let controller = null;

  function syncUrl() {
    const params = new URLSearchParams();
    if (state.q) params.set("q", state.q);
    if (state.phase) params.set("phase", state.phase);
    const qs = params.toString();
    history.replaceState(null, "", qs ? `?${qs}` : location.pathname);
  }

  async function run() {
    if (controller) controller.abort();
    controller = new AbortController();

    const params = new URLSearchParams();
    if (state.q) params.set("q", state.q);
    if (state.phase) params.set("phase", state.phase);

    try {
      const res = await fetch(`/api/search?${params}`, { signal: controller.signal });
      if (!res.ok) throw new Error(String(res.status));
      const data = await res.json();
      state.results = data.results;
      state.phases = data.phases;
      render();
      syncUrl();
    } catch (err) {
      if (err.name === "AbortError") return;
      heading.textContent = "Search is unavailable. Check that the PT Helper service is still running.";
    }
  }

  const queueSearch = () => { clearTimeout(timer); timer = setTimeout(run, 120); };

  // ---------- events ----------

  input.addEventListener("input", () => { state.q = input.value.trim(); queueSearch(); });

  input.form.addEventListener("submit", e => { e.preventDefault(); state.q = input.value.trim(); run(); });

  nav.addEventListener("click", e => {
    const btn = e.target.closest(".phase-btn");
    if (!btn) return;
    state.phase = btn.dataset.phase;
    run();
  });

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      const ta = el("textarea");
      ta.value = text;
      ta.className = "visually-hidden";
      document.body.append(ta);
      ta.select();
      document.execCommand("copy");
      ta.remove();
    }
  }

  list.addEventListener("click", async e => {
    if (e.target.closest("[data-action='clear']")) {
      input.value = "";
      state.q = "";
      run();
      input.focus();
      return;
    }
    const btn = e.target.closest(".copy");
    if (!btn) return;
    const code = btn.parentElement.querySelector("code");
    await copyText(code.textContent);
    btn.textContent = "Copied";
    btn.classList.add("is-copied");
    setTimeout(() => { btn.textContent = "Copy"; btn.classList.remove("is-copied"); }, 1400);
  });

  targetInput.value = target;
  targetInput.addEventListener("input", () => {
    target = targetInput.value.trim();
    store.set("pt-target", target);
    list.querySelectorAll("code[data-raw]").forEach(code => {
      code.textContent = withTarget(code.dataset.raw);
    });
  });

  document.addEventListener("keydown", e => {
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);
    if (e.key === "/" && !typing && !e.metaKey && !e.ctrlKey && !e.altKey) {
      e.preventDefault();
      input.focus();
      input.select();
    } else if (e.key === "Escape" && document.activeElement === input && input.value) {
      input.value = "";
      state.q = "";
      run();
    }
  });

  render();
})();
