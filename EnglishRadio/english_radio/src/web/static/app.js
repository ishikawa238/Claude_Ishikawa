// EnglishRadio 管理画面の共通処理
async function api(method, url, body) {
  const opts = { method, headers: { "Content-Type": "application/json" } };
  if (body !== undefined) opts.body = JSON.stringify(body);
  let data = {};
  try {
    const res = await fetch(url, opts);
    data = await res.json().catch(() => ({}));
    if (!res.ok && data.ok === undefined) data = { ok: false, message: `エラー (HTTP ${res.status})` };
  } catch (e) {
    data = { ok: false, message: "管理画面のサーバーに接続できません" };
  }
  return data;
}

function toast(message, isError) {
  const el = document.getElementById("toast");
  el.textContent = message;
  el.className = "show" + (isError ? " error" : "");
  clearTimeout(el._timer);
  el._timer = setTimeout(() => { el.className = ""; }, 5000);
}

// data-post="URL" を持つボタンは、押すとその URL に POST する
document.addEventListener("click", async (ev) => {
  const btn = ev.target.closest("[data-post]");
  if (!btn) return;
  if (btn.dataset.confirm && !confirm(btn.dataset.confirm)) return;
  btn.disabled = true;
  const body = btn.dataset.body ? JSON.parse(btn.dataset.body) : {};
  const data = await api("POST", btn.dataset.post, body);
  toast(data.message || (data.ok ? "OK" : "失敗しました"), !data.ok);
  btn.disabled = false;
  if (data.ok && btn.dataset.reload) setTimeout(() => location.reload(), 1500);
});

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text == null ? "" : String(text);
  return div.innerHTML;
}
