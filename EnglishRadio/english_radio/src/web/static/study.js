// 学習ページ（字幕同期・和訳切替・1文リピート）と単語帳（覚えた・復習カード）
(function () {
  "use strict";

  const readJson = (id) => {
    const el = document.getElementById(id);
    return el ? JSON.parse(el.textContent) : null;
  };
  const escapeRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

  // ---------------- 学習ページ ----------------
  function initStudy() {
    const audio = document.getElementById("player");
    const box = document.getElementById("transcript");
    if (!audio || !box) return;
    const segs = Array.from(box.querySelectorAll(".seg")).map((el) => ({
      el, idx: Number(el.dataset.idx), start: Number(el.dataset.start), end: Number(el.dataset.end),
    }));
    let current = null;
    let repeat = false;
    let follow = true;

    highlightTerms(segs);

    const findSeg = (t) => {
      // 二分探索で「開始 <= t」の最後の文を探す
      let lo = 0, hi = segs.length - 1, ans = null;
      while (lo <= hi) {
        const mid = (lo + hi) >> 1;
        if (segs[mid].start <= t + 0.05) { ans = segs[mid]; lo = mid + 1; } else { hi = mid - 1; }
      }
      return ans;
    };

    const setCurrent = (seg) => {
      if (seg === current) return;
      if (current) current.el.classList.remove("now");
      current = seg;
      if (!seg) return;
      seg.el.classList.add("now");
      if (follow) seg.el.scrollIntoView({ block: "center", behavior: "smooth" });
    };

    const playFrom = (seg) => {
      audio.currentTime = seg.start;
      setCurrent(seg);
      audio.play().catch(() => {});
    };

    audio.addEventListener("timeupdate", () => {
      if (repeat && current && audio.currentTime >= current.end - 0.05) {
        audio.currentTime = current.start; // 同じ文をくり返す
        return;
      }
      setCurrent(findSeg(audio.currentTime));
    });

    box.addEventListener("click", (ev) => {
      const el = ev.target.closest(".seg");
      if (!el) return;
      if (box.dataset.view === "quiz" && ev.target.closest(".ja")) {
        el.classList.toggle("reveal"); // 和訳を隠すモードでは、和訳部分のタップで表示
        return;
      }
      playFrom(segs.find((s) => s.el === el));
    });
    box.addEventListener("keydown", (ev) => {
      const el = ev.target.closest(".seg");
      if (el && ev.key === "Enter") playFrom(segs.find((s) => s.el === el));
    });

    document.querySelectorAll("[data-view]").forEach((btn) => {
      if (btn === box) return;
      btn.addEventListener("click", () => {
        box.dataset.view = btn.dataset.view;
        box.querySelectorAll(".reveal").forEach((el) => el.classList.remove("reveal"));
        activate(btn);
      });
    });
    document.querySelectorAll("[data-rate]").forEach((btn) => {
      btn.addEventListener("click", () => { audio.playbackRate = Number(btn.dataset.rate); activate(btn); });
    });
    toggleButton("repeat", (on) => { repeat = on; });
    toggleButton("follow", (on) => { follow = on; });

    // 単語をクリックすると、その単語が出てくる文から再生
    document.querySelectorAll(".term-link").forEach((btn) => {
      btn.addEventListener("click", () => {
        const seg = segs.find((s) => String(s.idx) === btn.dataset.seg);
        if (seg) playFrom(seg);
      });
    });

    // 単語帳から #s12 で来たら、その文の位置に合わせておく（自動再生はしない）
    const m = location.hash.match(/^#s(\d+)$/);
    if (m) {
      const seg = segs.find((s) => s.idx === Number(m[1]));
      if (seg) {
        const jump = () => { audio.currentTime = seg.start; setCurrent(seg); };
        if (audio.readyState >= 1) jump(); else audio.addEventListener("loadedmetadata", jump, { once: true });
      }
    }
  }

  function activate(btn) {
    btn.parentElement.querySelectorAll(".chip").forEach((b) => b.classList.toggle("active", b === btn));
  }

  function toggleButton(id, onChange) {
    const btn = document.getElementById(id);
    if (!btn) return;
    btn.addEventListener("click", () => {
      const on = btn.getAttribute("aria-pressed") !== "true";
      btn.setAttribute("aria-pressed", String(on));
      btn.classList.toggle("active", on);
      onChange(on);
    });
  }

  // 抜き出した単語を、その単語が出てくる文の中で下線表示する（テキストノードだけを書き換える）
  function highlightTerms(segs) {
    const terms = readJson("vocab-data") || [];
    const segIdx = readJson("vocab-segs") || [];
    terms.forEach((term, i) => {
      const target = segs.find((s) => s.idx === segIdx[i]);
      if (!target || !term) return;
      const en = target.el.querySelector(".en");
      const re = new RegExp(`\\b${escapeRe(term)}`, "i");
      for (const node of Array.from(en.childNodes)) {
        if (node.nodeType !== Node.TEXT_NODE) continue;
        const match = node.textContent.match(re);
        if (!match) continue;
        const after = node.splitText(match.index);
        after.splitText(match[0].length);
        const mark = document.createElement("mark");
        mark.className = "term";
        mark.textContent = after.textContent;
        after.replaceWith(mark);
        break;
      }
    });
  }

  // ---------------- 文字起こし待ちの自動更新 ----------------
  function initPolling() {
    const panel = document.getElementById("no-transcript");
    if (!panel) return;
    const guid = panel.dataset.guid;
    let timer = null;
    const poll = async () => {
      const data = await api("GET", `/api/study/status?guid=${encodeURIComponent(guid)}`);
      if (data.status === "done") location.reload();
      else if (!data.running && timer) { clearInterval(timer); timer = null; location.reload(); }
    };
    const start = () => { if (!timer) timer = setInterval(poll, 10000); };
    if (panel.dataset.running === "true") start();
    panel.querySelector("[data-poll]")?.addEventListener("click", () => setTimeout(start, 1500));
  }

  // ---------------- 「覚えた」ボタン ----------------
  document.addEventListener("click", async (ev) => {
    const btn = ev.target.closest(".known-btn");
    if (!btn) return;
    const item = btn.closest("[data-vocab-id]");
    const known = btn.dataset.known === "true";
    btn.disabled = true;
    const data = await api("PUT", `/api/vocab/${item.dataset.vocabId}`, { known });
    btn.disabled = false;
    if (!data.ok) { toast(data.message || "保存できませんでした", true); return; }
    item.classList.toggle("known", known);
    btn.dataset.known = String(!known);
    btn.textContent = known ? "✅ 覚えた" : "覚えた";
  });

  // ---------------- 復習カード ----------------
  function initReview() {
    const words = readJson("review-data");
    if (!words || !words.length) return;
    const queue = words.slice();
    const retried = new Set();
    const $ = (id) => document.getElementById(id);
    let done = 0;
    let word = null;

    const show = () => {
      word = queue.shift();
      if (!word) {
        $("rv-card").hidden = true;
        document.querySelector(".review-actions").hidden = true;
        document.querySelector(".review-progress").hidden = true;
        $("rv-done").hidden = false;
        return;
      }
      $("rv-count").textContent = String(Math.min(done + 1, words.length));
      $("rv-kind").textContent = word.kind === "phrase" ? "フレーズ" : "単語";
      $("rv-term").textContent = word.term;
      $("rv-meaning").textContent = word.meaning_ja;
      $("rv-ex-en").textContent = word.example_en || "";
      $("rv-ex-ja").textContent = word.example_ja || "";
      const hash = word.segment_idx === null ? "" : `#s${word.segment_idx}`;
      $("rv-link").href = `/study/episode?guid=${encodeURIComponent(word.guid)}${hash}`;
      $("rv-answer").hidden = true;
      $("rv-show").hidden = false;
      $("rv-ok").hidden = $("rv-ng").hidden = true;
      $("rv-card").focus();
    };

    const reveal = () => {
      $("rv-answer").hidden = false;
      $("rv-show").hidden = true;
      $("rv-ok").hidden = $("rv-ng").hidden = false;
    };

    const answer = async (ok) => {
      if (!word || $("rv-answer").hidden) return;
      const current = word;
      $("rv-ok").disabled = $("rv-ng").disabled = true;
      const data = await api("PUT", `/api/vocab/${current.id}`, { result: ok ? "ok" : "ng" });
      $("rv-ok").disabled = $("rv-ng").disabled = false;
      if (!data.ok) { toast(data.message || "保存できませんでした", true); return; }
      if (!ok && !retried.has(current.id)) {
        retried.add(current.id);
        queue.push(current); // 間違えた単語は最後にもう一度
      } else {
        done += 1;
      }
      show();
    };

    $("rv-show").addEventListener("click", reveal);
    $("rv-card").addEventListener("click", (ev) => { if (!ev.target.closest("a")) reveal(); });
    $("rv-ok").addEventListener("click", () => answer(true));
    $("rv-ng").addEventListener("click", () => answer(false));
    document.addEventListener("keydown", (ev) => {
      if (ev.key === " ") { ev.preventDefault(); reveal(); }
      else if (ev.key === "ArrowRight") answer(true);
      else if (ev.key === "ArrowLeft") answer(false);
    });
    show();
  }

  initStudy();
  initPolling();
  initReview();
})();
