/* Shared helpers: CSRF-aware fetch, toasts, confirm modal, theme, sidebar, notification bell, markdown-lite */
const csrfToken = () => document.querySelector('meta[name="csrf-token"]')?.content || "";

async function api(url, { method = "POST", json, form } = {}) {
  const headers = { "X-CSRFToken": csrfToken(), "X-Requested-With": "fetch" };
  let body;
  if (json !== undefined) { headers["Content-Type"] = "application/json"; body = JSON.stringify(json); }
  if (form) body = form;
  let res;
  try { res = await fetch(url, { method, headers, body, credentials: "same-origin" }); }
  catch (e) { throw new Error("Network error — is the server running?"); }
  let data = {};
  try { data = await res.json(); } catch (e) { /* non-JSON */ }
  if (res.status === 401) { window.location.href = "/login"; throw new Error("Session expired"); }
  if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
}

function toast(message, type = "info") {
  const box = document.getElementById("toasts"); if (!box) return;
  const colors = { success: "success", danger: "danger", warning: "warning", info: "primary" };
  const el = document.createElement("div");
  el.className = `toast align-items-center text-bg-${colors[type] || "primary"} border-0`;
  el.setAttribute("role", "alert");
  el.innerHTML = `<div class="d-flex"><div class="toast-body"></div><button type="button" class="btn-close btn-close-white me-2 m-auto" data-bs-dismiss="toast"></button></div>`;
  el.querySelector(".toast-body").textContent = message;
  box.appendChild(el);
  const t = new bootstrap.Toast(el, { delay: type === "danger" ? 7000 : 4500 }); t.show();
  el.addEventListener("hidden.bs.toast", () => el.remove());
}

function escapeHtml(s) { return String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }

function renderMarkdown(src) {            // safe: escape first, then apply formatting
  const blocks = []; let t = escapeHtml(src || "");
  t = t.replace(/```(\w*)\n?([\s\S]*?)```/g, (_, l, c) => { blocks.push(`<pre><code>${c}</code></pre>`); return `\u0000${blocks.length - 1}\u0000`; });
  const lines = t.split("\n"); let out = "", list = null;
  const close = () => { if (list) { out += `</${list}>`; list = null; } };
  for (let ln of lines) {
    ln = ln.replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>").replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>");
    let m;
    if ((m = ln.match(/^(#{1,4})\s+(.*)/))) { close(); out += `<h6 class="mt-2">${m[2]}</h6>`; }
    else if ((m = ln.match(/^\s*[-*]\s+(.*)/))) { if (list !== "ul") { close(); out += "<ul>"; list = "ul"; } out += `<li>${m[1]}</li>`; }
    else if ((m = ln.match(/^\s*\d+[.)]\s+(.*)/))) { if (list !== "ol") { close(); out += "<ol>"; list = "ol"; } out += `<li>${m[1]}</li>`; }
    else if ((m = ln.match(/^&gt;\s?(.*)/))) { close(); out += `<blockquote>${m[1]}</blockquote>`; }
    else if (ln.trim() === "") { close(); }
    else if (/^\u0000\d+\u0000$/.test(ln.trim())) { close(); out += ln.trim(); }
    else { close(); out += `<p>${ln}</p>`; }
  }
  close();
  return out.replace(/\u0000(\d+)\u0000/g, (_, i) => blocks[+i]);
}

function copyText(text) {
  const done = () => toast("Copied to clipboard", "success");
  if (navigator.clipboard?.writeText) navigator.clipboard.writeText(text).then(done, () => fallback());
  else fallback();
  function fallback() { const ta = document.createElement("textarea"); ta.value = text; document.body.appendChild(ta); ta.select(); document.execCommand("copy"); ta.remove(); done(); }
}

/* ---- confirm modal: <form data-confirm="Delete this?"> ---- */
document.addEventListener("submit", e => {
  const f = e.target; const msg = f.dataset?.confirm;
  if (!msg || f.dataset.confirmed) return;
  e.preventDefault();
  const modalEl = document.getElementById("confirmModal");
  modalEl.querySelector("[data-msg]").textContent = msg;
  const ok = modalEl.querySelector("[data-ok]");
  const modal = bootstrap.Modal.getOrCreateInstance(modalEl);
  const handler = () => { f.dataset.confirmed = "1"; modal.hide(); f.submit(); };
  ok.onclick = handler; modal.show();
});
document.addEventListener("submit", e => {         // prevent double submits
  const f = e.target; if (f.dataset.confirm && !f.dataset.confirmed) return;
  const b = f.querySelector('button[type=submit]:not([data-no-lock])');
  if (b && !f.dataset.noLock) setTimeout(() => { b.disabled = true; b.dataset.label = b.innerHTML; b.innerHTML = '<span class="spinner-border spinner-border-sm"></span>'; }, 0);
});

document.addEventListener("DOMContentLoaded", () => {
  (window.__flashes || []).forEach(([cat, msg]) => toast(msg, cat === "message" ? "info" : cat));

  // sidebar
  const body = document.body;
  if (localStorage.getItem("sidebar") === "collapsed") body.classList.add("sidebar-collapsed");
  document.getElementById("sidebarToggle")?.addEventListener("click", () => {
    if (window.innerWidth < 992) body.classList.toggle("sidebar-open");
    else { body.classList.toggle("sidebar-collapsed"); localStorage.setItem("sidebar", body.classList.contains("sidebar-collapsed") ? "collapsed" : "open"); }
  });
  document.addEventListener("click", e => { if (body.classList.contains("sidebar-open") && !e.target.closest(".sidebar") && !e.target.closest("#sidebarToggle")) body.classList.remove("sidebar-open"); });

  // theme
  document.getElementById("themeToggle")?.addEventListener("click", async () => {
    const root = document.documentElement; const next = root.dataset.bsTheme === "dark" ? "light" : "dark";
    root.dataset.bsTheme = next; document.getElementById("themeIcon").className = next === "dark" ? "fa-solid fa-sun" : "fa-solid fa-moon";
    try { await api("/api/theme", { json: { theme: next } }); } catch (e) { toast(e.message, "danger"); }
    window.dispatchEvent(new Event("themechange"));
  });

  // notification bell
  const bell = document.getElementById("bellList");
  if (bell) {
    const load = async () => {
      try {
        const d = await api("/api/notifications/summary", { method: "GET" });
        const badge = document.getElementById("bellCount");
        badge.textContent = d.unread > 99 ? "99+" : d.unread; badge.classList.toggle("d-none", d.unread === 0);
        bell.innerHTML = d.items.length ? d.items.map(n => `<a class="notif-item ${n.is_read ? "" : "unread"}" href="${n.link ? escapeHtml(n.link) : "/notifications"}">
          <div class="flex-grow-1"><div class="fw-semibold">${escapeHtml(n.title)}</div><div class="small text-muted">${escapeHtml(n.message || "")}</div><div class="small text-muted">${n.time}</div></div></a>`).join("")
          : `<div class="empty py-4"><i class="fa-regular fa-bell"></i><h6>You are all caught up</h6></div>`;
      } catch (e) { /* ignore transient errors */ }
    };
    load(); setInterval(load, 60000);
    document.getElementById("bellBtn").addEventListener("click", load);
  }
});

/* Promise-based confirm dialog for fetch-driven actions */
function confirmAction(message) {
  return new Promise(resolve => {
    const el = document.getElementById("confirmModal"); el.querySelector("[data-msg]").textContent = message;
    const modal = bootstrap.Modal.getOrCreateInstance(el); let answered = false;
    el.querySelector("[data-ok]").onclick = () => { answered = true; modal.hide(); resolve(true); };
    el.addEventListener("hidden.bs.modal", () => { if (!answered) resolve(false); }, { once: true });
    modal.show();
  });
}
