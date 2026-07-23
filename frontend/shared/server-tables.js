/* Page-specific adapters for the standard 20-row collection API. */
(() => {
  const esc = (value) =>
    String(value ?? "").replace(
      /[&<>"']/g,
      (char) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[char],
    );
  const money = (value) =>
    "HKD " +
    Number(value || 0).toLocaleString("en-HK", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    });
  const table = (rows, columns, action) =>
    rows.length
      ? `<div class="table-wrap"><table><thead><tr>${columns.map((c) => `<th>${c[0]}</th>`).join("")}${action ? "<th>操作</th>" : ""}</tr></thead><tbody>${rows.map((row) => `<tr>${columns.map((c) => `<td>${esc(c[1](row))}</td>`).join("")}${action ? `<td>${action(row)}</td>` : ""}</tr>`).join("")}</tbody></table></div>`
      : '<p class="caption">暫無紀錄。</p>';
  const observeVisible = (id, callback) => {
    const element = document.getElementById(id);
    if (!element) return;
    let started = false;
    const start = () => {
      if (!started && !element.classList.contains("hidden")) {
        started = true;
        callback();
      }
    };
    start();
    new MutationObserver(start).observe(element, {
      attributes: true,
      attributeFilter: ["class"],
    });
  };
  if (location.pathname === "/ai-fund")
    observeVisible("app", () => {
      VoteUI.serverPaged(
        document.getElementById("transactions"),
        "/api/ai-fund/transactions",
        (rows) =>
          table(rows, [
            ["編號", (r) => r.id],
            ["類型", (r) => r.transaction_type],
            ["狀態", (r) => r.status],
            ["Provider", (r) => r.provider],
            ["金額", (r) => money(r.amount_hkd)],
            ["提交者", (r) => r.created_by],
            ["時間", (r) => r.created_at],
          ]),
      ).catch(() => {});
      VoteUI.serverPaged(
        document.getElementById("usageTable"),
        "/api/ai-fund/usage",
        (rows) =>
          table(rows, [
            ["時間", (r) => r.created_at],
            ["用戶", (r) => r.user_id],
            ["功能", (r) => r.feature],
            ["模型", (r) => r.model_label],
            ["Provider", (r) => r.provider],
            ["估算成本", (r) => money(r.estimated_cost_hkd)],
            ["狀態", (r) => r.status],
          ]),
      ).catch(() => {});
      const pending = document.getElementById("pending");
      if (pending)
        VoteUI.serverPaged(
          pending,
          "/api/ai-fund/transactions?status=pending&transaction_type=member_deposit",
          (rows) =>
            table(
              rows,
              [
                ["編號", (r) => r.id],
                ["提交者", (r) => r.created_by],
                ["金額", (r) => money(r.amount_hkd)],
                ["付款方式", (r) => r.payment_method],
                ["Reference", (r) => r.reference_no],
                ["備註", (r) => r.note],
              ],
              (r) =>
                `<button data-confirm="${r.id}">確認</button> <button class="danger" data-reject="${r.id}">拒絕</button>`,
            ),
        ).catch(() => {});
    });
  if (location.pathname === "/ai-training")
    observeVisible("app", () => {
      const specs = [
        [
          "myRecordings",
          "my-recordings",
          [
            ["句子", (r) => r.script_id],
            ["狀態", (r) => r.status],
            ["時間", (r) => r.created_at],
            ["備註", (r) => r.review_note || ""],
          ],
        ],
        [
          "lexiconTable",
          "lexicon",
          [
            ["詞語", (r) => r.term],
            ["讀法", (r) => r.reading],
            ["粵拼", (r) => r.jyutping],
            ["類別", (r) => r.category],
          ],
        ],
        [
          "adminRecordings",
          "recordings",
          [
            ["提交者", (r) => r.speaker_user_id],
            ["句子", (r) => r.script_id],
            ["狀態", (r) => r.status],
            ["時間", (r) => r.created_at],
          ],
        ],
      ];
      specs.forEach(([id, kind, columns]) => {
        const target = document.getElementById(id);
        if (!target) return;
        let action = null;
        if (kind === "my-recordings")
          action = (r) =>
            `<audio controls preload="none" src="/api/ai-training/recordings/${r.id}/audio"></audio>`;
        if (kind === "recordings")
          action = (r) =>
            `<audio controls preload="none" src="/api/ai-training/recordings/${r.id}/audio"></audio> ${r.status === "pending" ? `<button data-rec="${r.id}" data-status="accepted">接受</button> <button class="danger" data-rec="${r.id}" data-status="rejected">拒絕</button>` : ""}`;
        if (kind === "lexicon")
          action = (r) => `<button data-edit="${esc(r.id)}">編輯</button>`;
        VoteUI.serverPaged(
          target,
          "/api/ai-training/collection/" + kind,
          (rows) => {
            if (kind === "lexicon")
              window.__lexiconPage = Object.fromEntries(
                rows.map((r) => [String(r.id), r]),
              );
            return table(rows, columns, action);
          },
        ).catch(() => {});
      });
      document.addEventListener(
        "click",
        (event) => {
          const id = event.target.closest("[data-edit]")?.dataset.edit,
            row = window.__lexiconPage?.[id];
          if (!row) return;
          event.stopImmediatePropagation();
          [
            ["lexiconId", "id"],
            ["term", "term"],
            ["reading", "reading"],
            ["jyutping", "jyutping"],
            ["example", "example"],
            ["lexNote", "note"],
            ["lexCategory", "category"],
          ].forEach(([target, key]) => {
            const el = document.getElementById(target);
            if (el) el.value = row[key] || "";
          });
        },
        true,
      );
    });
  if (location.pathname === "/dev-settings")
    observeVisible("app", () => {
      const bugLabels = {
        open: "待處理",
        investigating: "調查中",
        fixed: "已修正",
        not_reproducible: "未能重現",
        duplicate: "重複回報",
        closed: "已關閉",
      };
      VoteUI.serverPaged(
        document.getElementById("accountsTable"),
        "/api/developer/collection/accounts",
        (rows) =>
          table(
            rows,
            [
              ["帳戶", (r) => r.user_id],
              ["狀態", (r) => r.account_status],
              ["最後登入", (r) => r.last_login_at || "—"],
              ["登入權限", (r) => (r.login_disabled ? "已停用" : "正常")],
            ],
            (r) =>
              `<button data-reset="${esc(r.user_id)}">重設密碼</button> <button data-access="${esc(r.user_id)}" data-disabled="${!r.login_disabled}" class="${r.login_disabled ? "" : "danger"}">${r.login_disabled ? "重新啟用" : "停用帳戶"}</button>`,
          ),
      ).catch(() => {});
      VoteUI.serverPaged(
        document.getElementById("bugsList"),
        "/api/developer/collection/bugs",
        (rows) =>
          rows
            .map(
              (r) =>
                `<details class="card report" ${["open", "investigating"].includes(r.status) ? "open" : ""}><summary>#${r.id} ${esc(r.affected_page)}｜${esc(bugLabels[r.status] || r.status)}｜${esc(r.reporter_user_id || "未知委員")}</summary><p class="caption">建立：${esc(r.created_at || "—")}｜更新：${esc(r.updated_at || "—")}｜解決：${esc(r.resolved_at || "—")}</p><p><b>裝置／瀏覽器：</b><br>${esc(r.device_info || "未提供")}</p><p><b>重現步驟：</b><br>${esc(r.reproduction_steps)}</p><p><b>預期：</b><br>${esc(r.expected_result || "未提供")}</p><p><b>實際：</b><br>${esc(r.actual_result)}</p><p><b>補充：</b><br>${esc(r.extra_notes || "未提供")}</p><label>狀態<select data-field="status">${["open", "investigating", "fixed", "not_reproducible", "duplicate", "closed"].map((x) => `<option value="${x}" ${r.status === x ? "selected" : ""}>${bugLabels[x]}</option>`).join("")}</select></label><label>修正版本<input data-field="version" value="${esc(r.fixed_version || "")}"></label><label>回覆委員<textarea data-field="reply">${esc(r.developer_reply || "")}</textarea></label><button data-bug="${r.id}" class="primary">更新回覆</button></details>`,
            )
            .join("") || '<section class="card">暫時未有 Bug 回報。</section>',
      ).catch(() => {});
    });
  if (location.pathname === "/registration-admin")
    observeVisible("app", () => {
      const load = () => {
        const edition = document.getElementById("filterEdition").value,
          status = document.getElementById("filterStatus").value,
          target = document.querySelector("#records").closest(".table-wrap");
        VoteUI.serverPaged(
          target,
          `/api/registration-admin/records?edition=${encodeURIComponent(edition)}&status=${encodeURIComponent(status)}`,
          (rows) => {
            document
              .getElementById("recordsEmpty")
              .classList.toggle("hidden", rows.length > 0);
            document
              .getElementById("recordsApp")
              .classList.toggle("hidden", !rows.length);
            document.getElementById("registrationId").innerHTML = rows
              .map(
                (r) =>
                  `<option value="${r.id}">${r.id} - ${esc(r.team_name)}</option>`,
              )
              .join("");
            return `<table><thead><tr>${["編號", "屆數", "隊名", "主辯", "一副", "二副", "結辯", "聯絡人", "班別", "聯絡電話", "狀態", "提交時間", "更新時間"].map((x) => `<th>${x}</th>`).join("")}</tr></thead><tbody id="records">${rows.map((r) => `<tr>${[r.id, r.competition_edition, r.team_name, r.main_debater_name, r.first_deputy_name, r.second_deputy_name, r.closing_debater_name, r.contact_name, r.contact_class, r.contact_phone, r.status_label, r.submitted_at, r.updated_at].map((x) => `<td>${esc(x)}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
          },
        ).catch(() => {});
      };
      load();
      ["filterEdition", "filterStatus"].forEach((id) =>
        document.getElementById(id).addEventListener("change", load),
      );
      document.getElementById("exportCsv").addEventListener(
        "click",
        (event) => {
          event.stopImmediatePropagation();
          location.href = `/api/registration-admin/export?edition=${encodeURIComponent(document.getElementById("filterEdition").value)}&status=${encodeURIComponent(document.getElementById("filterStatus").value)}`;
        },
        true,
      );
    });
  if (location.pathname === "/lateness-fund")
    observeVisible("app", () => {
      const load = () => {
        const year = document.getElementById("year").value,
          ecols = [
            ["日期", (r) => String(r.expense_date || "").slice(0, 10)],
            ["金額", (r) => money(r.amount_hkd)],
            ["備註", (r) => r.note],
            ["記錄人", (r) => r.created_by],
          ];
        VoteUI.serverPaged(
          document.getElementById("summary"),
          `/api/lateness-fund/summary?year=${year}`,
          (rows) =>
            table(rows, [
              ["排名", (r) => r.late_rank],
              ["帳戶", (r) => r.member_user_id],
              ["遲到次數", (r) => r.late_count],
              ["累計遲到分鐘", (r) => r.total_late_minutes],
              ["應繳罰款", (r) => money(r.penalty_amount)],
              ["已繳金額", (r) => money(r.paid_amount)],
              ["結餘", (r) => money(r.balance)],
            ]),
        ).catch(() => {});
        VoteUI.serverPaged(
          document.getElementById("records"),
          `/api/lateness-fund/records?year=${year}`,
          (rows) => {
            window.__latenessPage = Object.fromEntries(
              rows.map((r) => [String(r.id), r]),
            );
            return table(
              rows,
              [
                ["日期", (r) => String(r.late_date || "").slice(0, 10)],
                ["帳戶", (r) => r.member_user_id],
                ["分鐘", (r) => r.late_minutes],
                ["第幾次", (r) => r.late_no],
                ["應繳", (r) => money(r.penalty_amount)],
                ["已繳", (r) => money(r.paid_amount)],
                ["備註", (r) => r.note],
              ],
              (r) =>
                `<button data-paid="${r.id}">更新已繳</button> <button class="danger" data-delete-record="${r.id}">刪除</button>`,
            );
          },
        ).catch(() => {});
        VoteUI.serverPaged(
          document.getElementById("expenses"),
          `/api/lateness-fund/expenses?year=${year}`,
          (rows) =>
            table(
              rows,
              ecols,
              (r) =>
                `<button class="danger" data-delete-expense="${r.id}">刪除</button>`,
            ),
        ).catch(() => {});
        VoteUI.serverPaged(
          document.getElementById("expensesOverview"),
          `/api/lateness-fund/expenses?year=${year}`,
          (rows) => table(rows, ecols),
        ).catch(() => {});
      };
      const preview = () => {
        const year = document.getElementById("year").value,
          member = document.getElementById("member").value,
          minutes = Number(document.getElementById("minutes").value || 0);
        fetch(
          `/api/lateness-fund/member-count?year=${year}&member=${encodeURIComponent(member)}`,
          { credentials: "same-origin" },
        )
          .then((r) => r.json())
          .then((d) => {
            const n = d.count + 1;
            document.getElementById("preview").textContent =
              `按現有紀錄計算，今次是該帳戶於本年度第 ${n} 次遲到，應繳 ${money(n * minutes)}。`;
          });
      };
      load();
      preview();
      document.getElementById("year").addEventListener("change", () =>
        setTimeout(() => {
          load();
          preview();
        }),
      );
      ["member", "minutes"].forEach((id) =>
        document.getElementById(id).addEventListener(
          id === "minutes" ? "input" : "change",
          (event) => {
            event.stopImmediatePropagation();
            preview();
          },
          true,
        ),
      );
      document.addEventListener(
        "click",
        (event) => {
          const id = event.target.closest("[data-paid]")?.dataset.paid,
            row = window.__latenessPage?.[id];
          if (!row) return;
          event.stopImmediatePropagation();
          const amount = prompt("更新已繳金額（HKD）", row.paid_amount);
          if (amount !== null)
            fetch("/api/lateness-fund/records/" + id, {
              method: "PATCH",
              credentials: "same-origin",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ amount: +amount }),
            }).then(load);
        },
        true,
      );
    });
  const sourceReturnLabels = {
    "/video-replay": {
      "/ghost-forum": "← 返回剛才帖文",
      "/team-history": "← 返回隊史 Timeline",
    },
    "/match-photos": {
      "/ghost-forum": "← 返回剛才帖文",
      "/team-history": "← 返回隊史 Timeline",
    },
    "/team-history": {
      "/ghost-forum": "← 返回剛才帖文",
    },
  };
  if (sourceReturnLabels[location.pathname]) {
    const sourceReturn = document.getElementById("sourceReturn"),
      returnTo = new URLSearchParams(location.search).get("return_to"),
      labels = sourceReturnLabels[location.pathname];
    if (sourceReturn && returnTo) {
      try {
        const target = new URL(returnTo, location.origin);
        if (target.origin === location.origin && labels[target.pathname]) {
          sourceReturn.href = `${target.pathname}${target.search}${target.hash}`;
          sourceReturn.textContent = labels[target.pathname];
          sourceReturn.classList.remove("hidden");
          sourceReturn.addEventListener("click", (event) => {
            try {
              const referrer = new URL(document.referrer);
              if (
                history.length > 1 &&
                referrer.origin === target.origin &&
                referrer.pathname === target.pathname
              ) {
                event.preventDefault();
                history.back();
              }
            } catch (_error) {
              // Follow the validated fallback href when referrer is unavailable.
            }
          });
        }
      } catch (_error) {
        // Invalid or external return targets leave only the home link visible.
      }
    }
  }
  if (location.pathname === "/video-replay")
    observeVisible("app", () => {
      let current = "";
      const load = () => {
        const id = new URL(location.href).searchParams.get("video_id");
        if (!id || id === current) return;
        current = id;
        VoteUI.serverPaged(
          document.getElementById("comments"),
          `/api/video-replay/comments?video_id=${id}`,
          (rows, meta) => {
            document.getElementById("commentTitle").textContent =
              `留言區（共 ${meta.total} 筆）`;
            return (
              rows
                .map(
                  (c) =>
                    `<div class="comment"><div class="caption"><strong>${esc(c.user_id)}</strong>　${esc(c.created_at)}</div>${esc(c.comment_text)}</div>`,
                )
                .join("") || '<div class="caption">暫時未有留言。</div>'
            );
          },
        ).catch(() => {
          current = "";
        });
      };
      load();
      new MutationObserver(load).observe(document.getElementById("title"), {
        childList: true,
      });
    });
  if (location.pathname === "/match-photos")
    observeVisible("app", () => {
      const target = document.getElementById("gallery"),
        lightbox = document.getElementById("photoLightbox"),
        lightboxClose = document.getElementById("photoLightboxClose"),
        lightboxImage = document.getElementById("photoLightboxImage"),
        lightboxTitle = document.getElementById("photoLightboxTitle"),
        lightboxStatus = document.getElementById("photoLightboxStatus"),
        showPhotoNotice = (message, isError = false) => {
          const notice = document.getElementById("toast");
          notice.textContent = message;
          notice.classList.toggle("err", isError);
          notice.classList.remove("hidden");
          window.clearTimeout(notice._photoEditTimer);
          notice._photoEditTimer = window.setTimeout(
            () => notice.classList.add("hidden"),
            3200,
          );
        },
        openPhotoLightbox = (trigger) => {
          lastPhotoTrigger = trigger;
          lightboxImage.removeAttribute("src");
          lightboxImage.classList.remove("hidden");
          lightboxImage.alt = trigger.querySelector("img")?.alt || "";
          lightboxTitle.textContent = trigger.dataset.photoTitle || "";
          lightboxStatus.textContent = "載入原圖中…";
          lightboxStatus.classList.remove("hidden", "err");
          lightbox.classList.remove("hidden");
          lightbox.setAttribute("aria-hidden", "false");
          document.body.classList.add("photo-lightbox-open");
          lightboxImage.src = trigger.dataset.photoSrc;
          lightboxClose.focus();
        },
        closePhotoLightbox = () => {
          if (lightbox.classList.contains("hidden")) return;
          lightbox.classList.add("hidden");
          lightbox.setAttribute("aria-hidden", "true");
          lightboxImage.removeAttribute("src");
          document.body.classList.remove("photo-lightbox-open");
          lastPhotoTrigger?.focus();
          lastPhotoTrigger = null;
        },
        editAlbumOptions = (photo) => {
          const currentVideo =
            photo.match_video_id == null ? "" : String(photo.match_video_id);
          let currentIsAllowed = false;
          const options = [...document.getElementById("album").options]
            .map((option) => {
              const video = option.dataset.video || "",
                selected =
                  option.value === photo.album_label &&
                  video === currentVideo,
                sameLabelDifferentVideo =
                  option.value === photo.album_label &&
                  video !== currentVideo,
                optionText = sameLabelDifferentVideo && video
                  ? `${option.textContent}（片段 #${video}）`
                  : option.textContent;
              currentIsAllowed ||= selected;
              return `<option value="${esc(option.value)}" data-video="${esc(video)}" ${selected ? "selected" : ""}>${esc(optionText)}</option>`;
            })
            .join("");
          const oldLink = currentVideo
            ? `片段 #${currentVideo}`
            : "舊連結";
          return `${currentIsAllowed ? "" : `<option value="${esc(photo.album_label)}" data-video="${esc(currentVideo)}" selected>原有：${esc(photo.album_label)}（${esc(oldLink)}）</option>`}${options}`;
        },
        photoEditor = (photo) =>
          photo.can_edit
            ? `<details><summary>編輯資料</summary><form class="photo-edit-form" data-photo-id="${esc(photo.id)}"><label>所屬場次</label><select name="album_label" required>${editAlbumOptions(photo)}</select><label>相片日期（可留空）</label><input name="photo_date" type="date" value="${esc(photo.photo_date)}"><label>圖片標題（可留空）</label><input name="photo_title" maxlength="300" value="${esc(photo.photo_title)}"><label>圖片說明（可留空）</label><textarea name="caption" maxlength="2000" rows="3">${esc(photo.caption)}</textarea><button class="primary" type="submit">儲存修改</button></form><button class="danger" type="button" data-delete-photo="${esc(photo.id)}">永久刪除圖片</button></details>`
            : "",
        linkedPhotoId = new URLSearchParams(location.search).get("photo_id"),
        load = (page = 1) => {
          const sortMap = {
              "相片日期（舊至新）": "date_asc",
              "上載時間（新至舊）": "created_desc",
              "上載時間（舊至新）": "created_asc",
            },
            url = `/api/match-photos/photos?album=${encodeURIComponent(document.getElementById("filter").value || "全部")}&search=${encodeURIComponent(document.getElementById("search").value)}&sort=${sortMap[document.getElementById("sort").value] || "date_desc"}${linkedPhotoId ? `&photo_id=${encodeURIComponent(linkedPhotoId)}` : ""}`;
          return VoteUI.serverPaged(target, url, (rows, meta) => {
            const empty = document.getElementById("empty"),
              hasSearch = Boolean(
                document.getElementById("search").value.trim(),
              ),
              hasAlbumFilter =
                document.getElementById("filter").value !== "全部";
            empty.textContent =
              hasSearch || hasAlbumFilter || linkedPhotoId
                ? "目前未有符合搜尋條件的圖片。"
                : "目前未有已上載的比賽圖片。";
            empty.classList.toggle("hidden", meta.total > 0);
            document.getElementById("galleryApp").classList.remove("hidden");
            document.getElementById("count").textContent =
              `共 ${meta.total} 張圖片`;
            target.className =
              document.getElementById("view").value === "縮圖牆"
                ? "thumbs"
                : "standard";
            return rows
              .map((p) => {
                const title =
                    p.photo_title || p.file_name || `match-photo-${p.id}.jpg`,
                  image = `/api/match-photos/image/${p.id}`,
                  thumbnail = `${image}?thumbnail=1`,
                  date = p.photo_date || "未設定";
                return `<article class="photo" id="photo-${esc(p.id)}"><button class="photo-preview" type="button" data-photo-src="${image}" data-photo-title="${esc(title)}" aria-haspopup="dialog" aria-label="放大查看：${esc(title)}"><img loading="lazy" decoding="async" src="${thumbnail}" alt="${esc(title)}"></button><div class="photo-title">${esc(title)}</div><div class="photo-meta">${esc(p.album_label)} ｜ ${esc(date)} ｜ ${esc(p.uploaded_by || "未設定")}</div>${p.caption ? `<p>${esc(p.caption)}</p>` : ""}<div class="photo-actions"><a href="${image}?download=1">下載原圖</a></div>${photoEditor(p)}</article>`;
              })
              .join("");
          }, page);
        };
      let lastPhotoTrigger = null;
      load();
      target.addEventListener("click", async (event) => {
        const deleteButton = event.target.closest("[data-delete-photo]");
        if (deleteButton) {
          const photoId = deleteButton.dataset.deletePhoto;
          if (
            !window.confirm(
              "永久刪除這張圖片？原圖、縮圖，以及歷史／討論區內的圖片連結都會移除，不能復原。",
            )
          )
            return;
          deleteButton.disabled = true;
          try {
            const response = await fetch(
                `/api/match-photos/photos/${encodeURIComponent(photoId)}`,
                { method: "DELETE", credentials: "same-origin" },
              ),
              data = await response.json().catch(() => ({}));
            if (!response.ok)
              throw new Error(data.detail || `HTTP ${response.status}`);
            showPhotoNotice(`☑️ ${data.message || "圖片已永久刪除。"}`);
            await load(1);
          } catch (error) {
            showPhotoNotice(error.message || "未能刪除圖片。", true);
          } finally {
            if (deleteButton.isConnected) deleteButton.disabled = false;
          }
          return;
        }
        const trigger = event.target.closest(".photo-preview");
        if (!trigger) return;
        openPhotoLightbox(trigger);
      });
      lightboxClose.addEventListener("click", closePhotoLightbox);
      lightbox.addEventListener("click", (event) => {
        if (event.target === lightbox) closePhotoLightbox();
      });
      lightboxImage.addEventListener("load", () => {
        lightboxStatus.classList.add("hidden");
      });
      lightboxImage.addEventListener("error", () => {
        lightboxImage.classList.add("hidden");
        lightboxStatus.textContent = "未能載入原圖，請關閉後再試。";
        lightboxStatus.classList.add("err");
        lightboxStatus.classList.remove("hidden");
      });
      document.addEventListener("keydown", (event) => {
        if (
          event.key === "Tab" &&
          !lightbox.classList.contains("hidden")
        ) {
          event.preventDefault();
          lightboxClose.focus();
          return;
        }
        if (
          event.key === "Escape" &&
          !lightbox.classList.contains("hidden")
        ) {
          event.preventDefault();
          closePhotoLightbox();
        }
      });
      target.addEventListener("submit", async (event) => {
        const form = event.target.closest(".photo-edit-form");
        if (!form) return;
        event.preventDefault();
        const album = form.elements.album_label,
          option = album.selectedOptions[0],
          button = form.querySelector('button[type="submit"]');
        if (!option || !option.value) {
          showPhotoNotice("請重新選擇所屬場次。", true);
          return;
        }
        button.disabled = true;
        try {
          const response = await fetch(
              `/api/match-photos/photos/${encodeURIComponent(form.dataset.photoId)}`,
              {
                method: "PATCH",
                credentials: "same-origin",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                  album_label: option.value,
                  match_video_id: option.dataset.video
                    ? Number(option.dataset.video)
                    : null,
                  photo_date: form.elements.photo_date.value,
                  photo_title: form.elements.photo_title.value,
                  caption: form.elements.caption.value,
                }),
              },
            ),
            data = await response.json().catch(() => ({}));
          if (!response.ok)
            throw new Error(data.detail || `HTTP ${response.status}`);
          showPhotoNotice(`☑️ ${data.message || "圖片資料已更新。"}`);
          await load(1);
        } catch (error) {
          showPhotoNotice(error.message || "未能更新圖片資料。", true);
        } finally {
          if (button.isConnected) button.disabled = false;
        }
      });
      ["filter", "sort", "view"].forEach((id) =>
        document.getElementById(id).addEventListener(
          "change",
          (event) => {
            event.stopImmediatePropagation();
            load();
          },
          true,
        ),
      );
      let searchTimer = null;
      const scheduleSearch = () => {
        window.clearTimeout(searchTimer);
        searchTimer = window.setTimeout(() => load(), 180);
      };
      document.getElementById("search").addEventListener(
        "input",
        (event) => {
          event.stopImmediatePropagation();
          if (event.isComposing) return;
          scheduleSearch();
        },
        true,
      );
      document.getElementById("search").addEventListener(
        "compositionend",
        (event) => {
          event.stopImmediatePropagation();
          scheduleSearch();
        },
        true,
      );
      window.addEventListener("match-photos:uploaded", () => load(1));
    });
  if (location.pathname === "/match-photos")
    observeVisible("app", () => {
      const filter = document.getElementById("filter"),
        labels = [...document.getElementById("album").options].map(
          (x) => x.value,
        );
      filter.innerHTML = ["全部", ...new Set(labels)]
        .map((x) => `<option>${esc(x)}</option>`)
        .join("");
      filter.dispatchEvent(new Event("change", { bubbles: true }));
    });
})();
