(() => {
  "use strict";
  const $ = (id) => document.getElementById(id),
    esc = (v) =>
      String(v ?? "").replace(
        /[&<>"']/g,
        (c) =>
          ({
            "&": "&amp;",
            "<": "&lt;",
            ">": "&gt;",
            '"': "&quot;",
            "'": "&#39;",
          })[c],
      );
  let data,
    blob,
    rec,
    current,
    started = 0,
    recordedSeconds = 0,
    pendingAudio,
    pendingUpload,
    pendingLlm,
    suggestions = [],
    scriptPage = 1,
    recordStopTimer = null,
    skipped = new Set();
  const toast = (x) => VoteUI.toast($("toast"), x),
    busy = (x) => VoteUI.setBusy($("busy"), x),
    api = async (url, opt = {}) => {
      const r = await fetch(url, {
          credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          ...opt,
        }),
        d = await r.json().catch(() => ({}));
      if (r.status === 401) throw Error("未登入");
      if (!r.ok) {
        const e = Error(d.detail || "操作失敗");
        e.status = r.status;
        throw e;
      }
      return d;
    },
    confirmAsk = (title, text) =>
      new Promise((ok) => {
        const d = $("confirmDialog");
        $("confirmTitle").textContent = title;
        $("confirmText").textContent = text;
        d.returnValue = "";
        d.onclose = () => ok(d.returnValue === "ok");
        d.showModal();
      });
  const table = (rows, cols, action) =>
    rows.length
      ? `<div class="table-wrap"><table><thead><tr>${cols.map((x) => `<th>${x[0]}</th>`).join("")}${action ? "<th>操作</th>" : ""}</tr></thead><tbody>${rows.map((r) => `<tr>${cols.map((x) => `<td>${x[2] ? x[1](r) : esc(x[1](r))}</td>`).join("")}${action ? `<td>${action(r)}</td>` : ""}</tr>`).join("")}</tbody></table></div>`
      : '<p class="caption">暫無紀錄。</p>';
  const paged = (id, url, renderer, preservePage = false) => {
    const target = $(id),
      page = preservePage ? Number(target._voteServerSpec?.page || 1) : 1;
    return VoteUI.serverPaged(target, url, renderer, page).catch((e) =>
      toast("⚠️ " + e.message),
    );
  };
  function loadCollections() {
    paged("myRecordings", "/api/ai-training/collection/my-recordings", (rows) =>
      table(
        rows,
        [
          ["句子", (r) => r.script_id],
          ["狀態", (r) => r.status],
          ["時間", (r) => r.created_at],
          ["備註", (r) => r.review_note || ""],
        ],
        (r) =>
          `<audio controls preload="none" src="/api/ai-training/recordings/${r.id}/audio"></audio>`,
      ),
    );
    paged("myLlm", "/api/ai-training/collection/my-llm", (rows) =>
      table(
        rows,
        [
          ["類型", (r) => r.data_type],
          ["標題", (r) => r.title || ""],
          ["AI 預檢", (r) => r.ai_review_status],
          ["狀態", (r) => r.status],
          ["時間", (r) => r.created_at],
        ],
        (r) =>
          r.status === "pending"
            ? `<button data-withdraw-llm="${r.id}" class="danger">撤回</button>`
            : "",
      ),
    );
    paged("publicLexicon", "/api/ai-training/collection/lexicon", (rows) =>
      table(rows, [
        ["詞語", (r) => r.term],
        ["讀法", (r) => r.reading],
        ["粵拼", (r) => r.jyutping || ""],
        ["例句", (r) => r.example || ""],
        ["備註", (r) => r.note || ""],
        ["類別", (r) => r.category || ""],
      ]),
    );
    if (data.is_admin) {
      loadRecordings();
      paged(
        "adminLlm",
        "/api/ai-training/collection/submissions",
        (rows) =>
          table(
            rows,
            [
              ["提交者", (r) => r.submitted_by],
              ["類型", (r) => r.data_type],
              ["標題", (r) => r.title || ""],
              ["AI", (r) => r.ai_review_status],
              [
                "內容",
                (r) =>
                  `<details><summary>原文 / 預檢</summary><p>${esc(r.content_text)}</p></details>`,
                true,
              ],
              ["狀態", (r) => r.status],
            ],
            (r) =>
              r.status === "pending"
                ? `<textarea class="review-note" data-note="llm-${r.id}" placeholder="審核備註"></textarea><button data-review="llm" data-id="${r.id}" data-status="accepted">接受</button><button data-review="llm" data-id="${r.id}" data-status="rejected" class="danger">拒絕</button>`
                : "",
          ),
        true,
      );
      paged("lexiconTable", "/api/ai-training/collection/lexicon", (rows) => {
        window.lexPage = Object.fromEntries(rows.map((x) => [x.id, x]));
        return table(
          rows,
          [
            ["詞語", (r) => r.term],
            ["讀法", (r) => r.reading],
            ["粵拼", (r) => r.jyutping],
            ["類別", (r) => r.category],
          ],
          (r) => `<button data-edit-lex="${esc(r.id)}">編輯</button>`,
        );
      });
    }
  }
  function loadRecordings(resetPage = false) {
    const status = $("recordFilter").value,
      speaker = $("speakerFilter").value;
    paged(
      "adminRecordings",
      `/api/ai-training/admin/recordings?status=${encodeURIComponent(status)}&speaker=${encodeURIComponent(speaker)}`,
      (rows) =>
        table(
          rows,
          [
            ["提交者", (r) => r.speaker_user_id],
            ["句子", (r) => r.script_id],
            ["稿句", (r) => r.prompt_text],
            [
              "AI 預檢",
              (r) =>
                `${r.ai_review_status}${r.ai_transcript ? `｜${r.ai_transcript}` : ""}`,
            ],
            ["狀態", (r) => r.status],
            [
              "錄音",
              (r) =>
                `<audio controls preload="none" src="/api/ai-training/recordings/${r.id}/audio"></audio>`,
              true,
            ],
          ],
          (r) =>
            r.status === "pending"
              ? `<textarea class="review-note" data-note="recordings-${r.id}" placeholder="審核備註"></textarea><button data-review="recordings" data-id="${r.id}" data-status="accepted">接受</button><button data-review="recordings" data-id="${r.id}" data-status="rejected" class="danger">拒絕</button>`
              : "",
        ),
      !resetPage,
    );
  }
  function chooseScript() {
    const mode = $("scriptType").value,
      all = data.scripts.filter((s) => s.script_type === mode),
      done = new Set(
        data.my_recordings
          .filter((r) => ["pending", "accepted"].includes(r.status))
          .map((r) => r.script_id),
      );
    current = all.find((s) => !done.has(s.id) && !skipped.has(s.id));
    const n = all.filter((s) => done.has(s.id)).length;
    $("modeHelp").textContent =
      mode === "short"
        ? "系統會依次顯示你尚未錄製的練習短句，錄好一句便自動跳至下一句。"
        : "系統會依次顯示完整稿的一段段內容，逐段錄製、逐段提交。";
    $("recordProgress").innerHTML =
      `<p class="caption">已錄 ${n} / ${all.length}</p><div class="progress"><i style="width:${all.length ? (100 * n) / all.length : 0}%"></i></div>`;
    let meta = "";
    if (current && mode === "full") {
      const segments = all.filter(
          (s) => s.manuscript_id === current.manuscript_id,
        ),
        pos = segments.findIndex((s) => s.id === current.id) + 1;
      meta = `<div class="caption">稿件：${esc(current.manuscript_title || current.manuscript_id)}　·　第 ${pos} / ${segments.length} 段</div>`;
    }
    $("script").innerHTML = current
      ? `${meta}<div class="script"><b>請照讀</b><p>${esc(current.text)}</p><span class="caption">${esc(current.id)}</span></div>`
      : `<p class="caption">${skipped.size ? `其餘內容已全部錄畢；你本次跳過了 ${skipped.size} 句。` : "此模式的內容你已全部錄畢，感謝參與。"}</p>`;
    $("recordBtn").disabled = !current;
    $("skipBtn").disabled = !current;
    $("resetSkipped").classList.toggle("hidden", !skipped.size);
  }
  async function load() {
    busy(true);
    try {
      data = await api("/api/ai-training/data");
      $("login").classList.add("hidden");
      $("app").classList.remove("hidden");
      $("consentText").textContent = data.consent_text;
      $("rdPlan").innerHTML = SafeMarkdown.render(data.rd_plan || "");
      $("ttsBlocked").classList.toggle("hidden", data.is_allowed);
      $("ttsBlocked").textContent = data.is_admin
        ? "你並非 TTS 錄音收集名單成員；你仍可使用管理員分頁。"
        : "你暫時未獲加入 TTS 錄音收集名單；仍可於「LLM 文字資料提交」分頁提交辯論文字資料。";
      $("consent").classList.toggle(
        "hidden",
        !data.is_allowed || data.consented,
      );
      $("recorder").classList.toggle(
        "hidden",
        !data.is_allowed || !data.consented,
      );
      $("adminTab").classList.toggle("hidden", !data.is_admin);
      const budget = data.bandwidth_budget || {},
        storage = data.storage_budget || {},
        budgetEl = $("trainingBandwidthUsage"),
        stopGb = Number(storage.stop_bytes || 0) / 1e9,
        warnGb = Number(storage.warn_bytes || 0) / 1e9,
        fmtLimit = (value) =>
          value.toFixed(2).replace(/\.00$/, "").replace(/0$/, "");
      if (budgetEl)
        budgetEl.textContent = `本月系統已記錄約 ${(Number(budget.total_bytes || 0) / 1e9).toFixed(2)}GB；目前保護階段：${budget.stage || 0}。R2 約 ${(Number(storage.total_bytes || 0) / 1e9).toFixed(2)}GB / ${stopGb ? fmtLimit(stopGb) + "GB" : "未設定"}${storage.warning ? `，已進入${fmtLimit(warnGb)}GB警告區` : ""}。`;
      chooseScript();
      loadCollections();
      if (data.is_admin) loadAdmin();
    } catch (e) {
      if (e.message === "未登入") $("login").classList.remove("hidden");
      else toast("⚠️ " + e.message);
    } finally {
      busy(false);
    }
  }
  function renderScriptInventory() {
    const rows = (window.inventory?.scripts || []).filter(
        (x) => x.script_type !== "full",
      ),
      pages = Math.max(1, Math.ceil(rows.length / 5));
    scriptPage = Math.min(Math.max(1, scriptPage), pages);
    const shown = rows.slice((scriptPage - 1) * 5, scriptPage * 5);
    const cards =
      shown
        .map(
          (x) => `<div class="script">
            <p>${esc(x.text)}</p>
            <span class="caption">
              ${esc(x.id)}｜${esc(x.category)}｜${x.is_active ? "🟢 啟用中" : "⚪ 已停用"}
            </span>
            <div class="actions">
              <button data-active-type="scripts" data-active-id="${esc(x.id)}" data-active="${!x.is_active}">
                ${x.is_active ? "停用" : "重新啟用"}
              </button>
              <button data-edit-script="${esc(x.id)}">編輯</button>
            </div>
          </div>`,
        )
        .join("") || '<p class="caption">句庫為空。</p>';
    $("scriptInventory").innerHTML = `<details>
      <summary>✏️ 編輯 / 停用現有句子</summary>
      <p class="caption">
        共 ${rows.length} 句，每頁顯示 5 句；現為第 ${scriptPage} / ${pages} 頁。
      </p>
      <div class="review-list">${cards}</div>
      <nav class="pager">
        <button data-script-page="${scriptPage - 1}" ${scriptPage <= 1 ? "disabled" : ""}>上一頁</button>
        <span>第 ${scriptPage} / ${pages} 頁</span>
        <button data-script-page="${scriptPage + 1}" ${scriptPage >= pages ? "disabled" : ""}>下一頁</button>
      </nav>
    </details>`;
    document.querySelectorAll("[data-script-page]").forEach(
      (b) =>
        (b.onclick = () => {
          scriptPage = +b.dataset.scriptPage;
          renderScriptInventory();
        }),
    );
  }
  async function loadAdmin() {
    const [stats, inv, ready] = await Promise.all([
      api("/api/ai-training/admin/stats"),
      api("/api/ai-training/inventory"),
      api("/api/ai-training/readiness"),
    ]);
    $("recordStats").textContent =
      stats.recordings.map((x) => `${x.status}: ${x.count}`).join(" ｜ ") ||
      "暫無錄音";
    $("llmStats").textContent =
      stats.llm.map((x) => `${x.status}: ${x.count}`).join(" ｜ ") ||
      "暫無資料";
    $("readinessSummary").innerHTML =
      `<p>Consent：${esc(ready.consent_version)}｜生效讀音字典：${ready.active_lexicon} / ${ready.gates.tts_min_lexicon}｜固定Eval：${ready.active_eval_cases} / ${ready.gates.llm_eval_cases}</p>${(ready.speakers || []).map((s) => `<p><b>${esc(s.speaker_user_id)}</b>：accepted ${s.accepted_minutes || 0}分鐘（${s.accepted_clips || 0}段）｜v2 eligible ${s.eligible_clips || 0}段｜pending ${s.pending_minutes || 0}分鐘</p>`).join("") || "<p>暫無聲線資料。</p>"}`;
    window.inventory = inv;
    renderScriptInventory();
    $("manuscriptInventory").innerHTML =
      inv.manuscripts
        .map(
          (m) =>
            `<details><summary>${esc(m.title)}（${m.segments} 段）</summary><button data-active-type="manuscripts" data-active-id="${esc(m.id)}" data-active="${!m.is_active}">${m.is_active ? "停用整份" : "重新啟用整份"}</button></details>`,
        )
        .join("") || '<p class="caption">暫無完整稿。</p>';
  }
  function resetRecording() {
    if (recordStopTimer) {
      clearTimeout(recordStopTimer);
      recordStopTimer = null;
    }
    if (rec) {
      rec.onstop = null;
      try {
        rec.stop();
      } catch {}
      rec.stream?.getTracks().forEach((t) => t.stop());
      rec = null;
    }
    blob = null;
    pendingAudio = null;
    pendingUpload = null;
    recordedSeconds = 0;
    const preview = $("preview");
    if (preview.src.startsWith("blob:")) URL.revokeObjectURL(preview.src);
    preview.removeAttribute("src");
    preview.classList.add("hidden");
    $("submitRecord").disabled = true;
    $("manualAudio").classList.add("hidden");
    $("manualAudioConfirm").checked = false;
    $("recordBtn").textContent = "開始錄音";
    $("recordState").textContent = "選擇句子後開始錄音。";
  }
  const hexSha = async (b) =>
      [
        ...new Uint8Array(
          await crypto.subtle.digest("SHA-256", await b.arrayBuffer()),
        ),
      ]
        .map((x) => x.toString(16).padStart(2, "0"))
        .join(""),
    recordPayload = (manual) => ({
      script_id: current.id,
      mime_type: blob.type,
      duration_seconds: recordedSeconds,
      manual_review: manual,
    }),
    uploadRecordingR2 = async (p) => {
      const sha = await hexSha(blob),
        intent = await api("/api/ai-training/recordings/upload-intent", {
          method: "POST",
          body: JSON.stringify({
            script_id: p.script_id,
            mime_type: p.mime_type,
            byte_size: blob.size,
            sha256: sha,
          }),
        }),
        r = await fetch(intent.url, {
          method: "PUT",
          headers: intent.headers,
          body: blob,
        });
      if (!r.ok) throw Error(`R2 錄音上載失敗（HTTP ${r.status}）`);
      pendingUpload = { r2_upload_token: intent.upload_token };
      Object.assign(p, pendingUpload);
    };
  async function submitAudio(manual = false) {
    busy(true);
    try {
      const p = recordPayload(manual);
      if (pendingUpload) Object.assign(p, pendingUpload);
      else await uploadRecordingR2(p);
      if (!manual) {
        let check;
        try {
          check = await api("/api/ai-training/recordings/quality-check", {
            method: "POST",
            body: JSON.stringify(p),
          });
        } catch (e) {
          pendingAudio = { error: e.message };
          $("manualAudio").classList.remove("hidden");
          toast("⚠️ " + e.message);
          return;
        }
        if (!check.ok) {
          pendingAudio = check;
          toast("⚠️ " + check.message);
          return;
        }
        p.review_token = check.review_token;
      }
      await api("/api/ai-training/recordings", {
        method: "POST",
        body: JSON.stringify(p),
      });
      toast("✅ 錄音已提交，等待人工審核。");
      resetRecording();
      await load();
    } catch (e) {
      toast("⚠️ " + e.message);
    } finally {
      busy(false);
    }
  }
  document.querySelectorAll("[data-pane]").forEach(
    (b) =>
      (b.onclick = () => {
        document
          .querySelectorAll(".pane,.tabs>button[data-pane]")
          .forEach((x) => x.classList.remove("active"));
        $(b.dataset.pane).classList.add("active");
        b.classList.add("active");
      }),
  );
  document.querySelectorAll("[data-admin]").forEach(
    (b) =>
      (b.onclick = () => {
        document
          .querySelectorAll(".admin-pane,#adminTabs button")
          .forEach((x) => x.classList.remove("active"));
        $(b.dataset.admin).classList.add("active");
        b.classList.add("active");
      }),
  );
  $("loginForm").onsubmit = async (e) => {
    e.preventDefault();
    busy(true);
    try {
      await api("/api/committee/login", {
        method: "POST",
        body: JSON.stringify({
          user_id: $("user").value,
          password: $("password").value,
        }),
      });
      await load();
      toast("✅ 已登入。");
    } catch (x) {
      toast("⚠️ " + x.message);
    } finally {
      busy(false);
    }
  };
  $("consentBtn").onclick = async () => {
    if (!$("agree").checked) return toast("⚠️ 請先確認同意。");
    await api("/api/ai-training/consent", {
      method: "POST",
      body: '{"agreed":true}',
    });
    load();
  };
  $("scriptType").onchange = () => {
    resetRecording();
    skipped.clear();
    chooseScript();
  };
  $("skipBtn").onclick = () => {
    if (current) {
      resetRecording();
      skipped.add(current.id);
      chooseScript();
    }
  };
  $("resetSkipped").onclick = () => {
    resetRecording();
    skipped.clear();
    chooseScript();
  };
  $("recordBtn").onclick = async () => {
    try {
      if (rec) {
        rec.stop();
        return;
      }
      resetRecording();
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true }),
        chunks = [],
        maxSeconds = Number(data.limits.max_duration_seconds || 60),
        maxBytes = Number(data.limits.max_audio_bytes || 2 * 1024 * 1024);
      let chunkBytes = 0;
      const activeRecorder = new MediaRecorder(stream);
      rec = activeRecorder;
      activeRecorder.ondataavailable = (e) => {
        if (!e.data?.size) return;
        chunks.push(e.data);
        chunkBytes += e.data.size;
        if (chunkBytes > maxBytes && activeRecorder.state === "recording") {
          activeRecorder.stop();
        }
      };
      activeRecorder.onstop = () => {
        if (recordStopTimer) {
          clearTimeout(recordStopTimer);
          recordStopTimer = null;
        }
        recordedSeconds = Math.max(
          1,
          Math.round((Date.now() - started) / 1000),
        );
        blob = new Blob(chunks, { type: activeRecorder.mimeType });
        $("preview").src = URL.createObjectURL(blob);
        $("preview").classList.remove("hidden");
        const tooLong = recordedSeconds > maxSeconds;
        const tooLarge = blob.size > maxBytes;
        $("submitRecord").disabled = tooLong || tooLarge;
        $("recordState").textContent = tooLong
          ? `錄音太長，請控制在 ${maxSeconds} 秒內。`
          : tooLarge
            ? `錄音檔案太大，請縮短錄音至 ${(maxBytes / 1024 / 1024).toFixed(2)}MB 以內。`
            : `已錄音（${recordedSeconds} 秒｜約 ${Math.round(blob.size / 1024)} KB），請先試聽。`;
        stream.getTracks().forEach((t) => t.stop());
        if (rec === activeRecorder) rec = null;
        $("recordBtn").textContent = "重新錄音";
      };
      started = Date.now();
      activeRecorder.start(1000);
      recordStopTimer = setTimeout(() => {
        if (activeRecorder.state === "recording") activeRecorder.stop();
      }, maxSeconds * 1000);
      $("recordBtn").textContent = "停止錄音";
      $("recordState").textContent = "錄音中…";
    } catch (e) {
      toast("⚠️ 未能使用咪高峰：" + e.message);
    }
  };
  $("submitRecord").onclick = () => submitAudio(false);
  $("manualAudioSubmit").onclick = () =>
    $("manualAudioConfirm").checked
      ? submitAudio(true)
      : toast("⚠️ 請先自行試聽及確認。");
  $("withdraw").onclick = async () => {
    if (
      await confirmAsk("撤回錄音同意", "既有錄音會標記為 withdrawn，確定？")
    ) {
      await api("/api/ai-training/consent", { method: "DELETE" });
      load();
    }
  };
  const llmPayload = (manual) => ({
    data_type: $("dataType").value,
    side: $("llmSide").value,
    title: $("llmTitle").value,
    topic_text: $("llmTopic").value,
    content_text: $("llmContent").value,
    source_note: $("llmSource").value,
    anonymized: $("anonymized").checked,
    permission_confirmed: $("permission").checked,
    manual_review: manual,
  });
  async function submitLlm(manual = false) {
    busy(true);
    try {
      const result = await api("/api/ai-training/llm", {
        method: "POST",
        body: JSON.stringify(llmPayload(manual)),
      });
      if (result.ok === false) {
        toast("⚠️ " + result.message);
        return;
      }
      toast("✅ " + result.message);
      $("llmForm").reset();
      $("manualLlm").classList.add("hidden");
      loadCollections();
    } catch (e) {
      if (e.status === 503) {
        pendingLlm = true;
        $("manualLlm").classList.remove("hidden");
      }
      toast("⚠️ " + e.message);
    } finally {
      busy(false);
    }
  }
  $("llmForm").onsubmit = (e) => {
    e.preventDefault();
    submitLlm(false);
  };
  $("clearLlm").onclick = () => {
    $("llmForm").reset();
    $("manualLlm").classList.add("hidden");
  };
  $("manualLlmSubmit").onclick = () =>
    $("manualLlmConfirm").checked
      ? submitLlm(true)
      : toast("⚠️ 請先確認資料適合提交。");
  $("recordFilter").onchange = () => loadRecordings(true);
  $("speakerFilter").oninput = () => {
    const speaker = $("speakerFilter").value.trim();
    $("recordExport").href =
      "/api/ai-training/export/recordings.json" +
      (speaker ? "?speaker=" + encodeURIComponent(speaker) : "");
    loadRecordings(true);
  };
  $("lexiconForm").onsubmit = (e) =>
    saveForm(e, "/api/ai-training/lexicon", {
      lexicon_id: $("lexiconId").value,
      term: $("term").value,
      reading: $("reading").value,
      jyutping: $("jyutping").value,
      example: $("example").value,
      note: $("lexNote").value,
      category: $("lexCategory").value,
    });
  $("scriptForm").onsubmit = (e) =>
    saveForm(e, "/api/ai-training/scripts", {
      script_id: $("scriptId").value,
      category: $("scriptCategory").value,
      text: $("scriptText").value,
      sort_order: +$("sortOrder").value,
    });
  $("manuscriptText").oninput = () =>
    ($("segmentPreview").textContent =
      `預計約 ${Math.ceil($("manuscriptText").value.length / 35)} 段；實際按細標點及每段最多 35 字切分。`);
  $("manuscriptForm").onsubmit = (e) =>
    saveForm(e, "/api/ai-training/manuscripts", {
      title: $("manuscriptTitle").value,
      text: $("manuscriptText").value,
      category: "完整稿",
      active: true,
    });
  async function saveForm(e, url, p) {
    e.preventDefault();
    busy(true);
    try {
      await api(url, { method: "POST", body: JSON.stringify(p) });
      toast("✅ 已儲存。");
      e.target.reset();
      load();
    } catch (x) {
      toast("⚠️ " + x.message);
    } finally {
      busy(false);
    }
  }
  $("coverageBtn").onclick = async () => {
    busy(true);
    try {
      const x = await api("/api/ai-training/coverage/ai", { method: "POST" }),
        a = x.analysis || {},
        gaps = a.gaps || [],
        items = a.suggested_scripts || [];
      suggestions = { items, deactivate_ids: [] };
      const gapMarkup = gaps
        .map(
          (gap) =>
            `<li>⚠️ <b>${esc(gap.area || "")}</b>：${esc(gap.why || "")}</li>`,
        )
        .join("");
      const itemMarkup = items
        .map(
          (item, index) => `<label class="check">
            <input type="checkbox" data-suggestion="new" data-index="${index}">
            [${esc(item.category || "AI 建議")}] ${esc(item.text || "")}
          </label>`,
        )
        .join("");
      $("coverageResult").innerHTML = [
        a.overall ? `<div class="notice">${esc(a.overall)}</div>` : "",
        a.well_covered?.length
          ? `<p><b>已足夠覆蓋：</b>${esc(a.well_covered.join("、"))}</p>`
          : "",
        gaps.length ? `<ul>${gapMarkup}</ul>` : "",
        items.length ? `<p><b>建議新增句子：</b></p>${itemMarkup}` : "",
      ].join("");
      $("applySuggestions").classList.toggle("hidden", !items.length);
    } catch (e) {
      toast("⚠️ " + e.message);
    } finally {
      busy(false);
    }
  };
  $("regenerateBtn").onclick = async () => {
    busy(true);
    try {
      const x = await api("/api/ai-training/regenerate-suggestions", {
          method: "POST",
        }),
        p = x.plan || {},
        items = p.new_scripts || [],
        deactivate = p.deactivate_candidates || [];
      suggestions = {
        items,
        deactivate_ids: deactivate.map((x) => String(x.script_id || "")),
      };
      const itemMarkup = items
        .map(
          (item, index) => `<label class="check">
            <input type="checkbox" checked data-suggestion="new" data-index="${index}">
            [${esc(item.category || "AI 建議")}] ${esc(item.text || "")}
          </label>`,
        )
        .join("");
      const deactivateMarkup = deactivate
        .map(
          (item, index) => `<label class="check">
            <input type="checkbox" data-suggestion="deactivate" data-index="${index}">
            ${esc(item.script_id || "")} ${item.reason ? `｜${esc(item.reason)}` : ""}
          </label>`,
        )
        .join("");
      $("coverageResult").innerHTML = [
        p.overall ? `<div class="notice">${esc(p.overall)}</div>` : "",
        items.length
          ? `<p><b>建議新增句子（預設全選）：</b></p>${itemMarkup}`
          : "",
        deactivate.length
          ? `<p><b>建議停用（只限未錄音句子）：</b></p>${deactivateMarkup}`
          : "",
      ].join("");
      $("applySuggestions").classList.toggle(
        "hidden",
        !(items.length || deactivate.length),
      );
    } catch (e) {
      toast("⚠️ " + e.message);
    } finally {
      busy(false);
    }
  };
  $("applySuggestions").onclick = async () => {
    const items = [
        ...document.querySelectorAll('[data-suggestion="new"]:checked'),
      ]
        .map((x) => suggestions.items[+x.dataset.index])
        .filter(Boolean),
      deactivate_ids = [
        ...document.querySelectorAll('[data-suggestion="deactivate"]:checked'),
      ]
        .map((x) => suggestions.deactivate_ids[+x.dataset.index])
        .filter(Boolean);
    if (!items.length && !deactivate_ids.length)
      return toast("⚠️ 請先選擇要套用的變更。");
    const x = await api("/api/ai-training/suggestions/apply", {
      method: "POST",
      body: JSON.stringify({ items, deactivate_ids }),
    });
    toast(`✅ 已新增 ${x.added} 句、停用 ${x.deactivated} 句。`);
    load();
  };
  $("deactivateComplete").onclick = async () => {
    if (
      await confirmAsk(
        "停用已完成內容",
        "短句逐句判斷；完整稿只會在整份稿所有啟用段落均由全體指定錄音者完成後一併停用。確定繼續？",
      )
    ) {
      const x = await api("/api/ai-training/scripts/deactivate-complete", {
        method: "POST",
      });
      toast(`✅ 已停用 ${x.deactivated} 段內容。`);
      load();
    }
  };
  document.body.addEventListener("click", async (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    if (b.dataset.review) {
      const note =
        document.querySelector(
          `[data-note="${b.dataset.review}-${b.dataset.id}"]`,
        )?.value || "";
      await api(`/api/ai-training/${b.dataset.review}/${b.dataset.id}/review`, {
        method: "POST",
        body: JSON.stringify({ status: b.dataset.status, note }),
      });
      toast("✅ 已更新審核結果。");
      loadCollections();
    } else if (b.dataset.withdrawLlm) {
      if (await confirmAsk("撤回提交", "確定撤回這份待審核文字資料？")) {
        await api(`/api/ai-training/llm/${b.dataset.withdrawLlm}`, {
          method: "DELETE",
        });
        loadCollections();
      }
    } else if (b.dataset.activeType) {
      await api(
        `/api/ai-training/${b.dataset.activeType}/${encodeURIComponent(b.dataset.activeId)}/active`,
        {
          method: "PATCH",
          body: JSON.stringify({ active: b.dataset.active === "true" }),
        },
      );
      load();
    } else if (b.dataset.editLex) {
      const x = window.lexPage[b.dataset.editLex];
      [
        ["lexiconId", "id"],
        ["term", "term"],
        ["reading", "reading"],
        ["jyutping", "jyutping"],
        ["example", "example"],
        ["lexNote", "note"],
        ["lexCategory", "category"],
      ].forEach(([a, k]) => ($(a).value = x[k] || ""));
    } else if (b.dataset.editScript) {
      const x = window.inventory.scripts.find(
        (s) => s.id === b.dataset.editScript,
      );
      $("scriptId").value = x.id;
      $("scriptCategory").value = x.category;
      $("scriptText").value = x.text;
      $("sortOrder").value = x.sort_order || 0;
      $("scriptText").focus();
    }
  });
  load();
})();
