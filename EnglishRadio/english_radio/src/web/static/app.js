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

// アートワークカードを押すと詳細パネルを開く（▶ ボタンは除く）
function openDetail(card) {
  const d = JSON.parse(card.dataset.detail);
  const dlg = document.getElementById("detail");
  dlg.style.setProperty("--hero", d.color || "#2c2c38");
  document.getElementById("dlg-art").innerHTML = d.art
    ? `<img class="art" src="${escapeHtml(d.art)}" alt="">`
    : `<div class="art ph" style="background:linear-gradient(135deg, ${escapeHtml(d.color)}, #1b1b22)">${escapeHtml((d.show || "?").slice(0, 1))}</div>`;
  document.getElementById("dlg-show").textContent = d.show;
  document.getElementById("dlg-title").textContent = d.title;
  document.getElementById("dlg-meta").textContent =
    `配信 ${d.published} ・ ${d.minutes} ・ ${d.plays ? d.plays + "回再生" : "未再生"}`;
  document.getElementById("dlg-summary").textContent = d.summary || "紹介文はありません。";
  const play = document.getElementById("dlg-play");
  play.dataset.post = "/api/play/episode";
  play.dataset.body = JSON.stringify({ guid: d.guid });
  dlg.showModal();
}

document.addEventListener("click", (ev) => {
  const card = ev.target.closest(".ecard[data-detail]");
  if (card && !ev.target.closest("[data-post]")) openDetail(card);
});
document.addEventListener("keydown", (ev) => {
  const card = ev.target.closest && ev.target.closest(".ecard[data-detail]");
  if (card && ev.key === "Enter" && !ev.target.closest("[data-post]")) openDetail(card);
});
