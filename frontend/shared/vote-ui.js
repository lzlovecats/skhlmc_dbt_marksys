/* Shared interaction primitives for direct HTML pages. */
window.VoteUI = Object.freeze({
    PAGE_SIZE: 20,
    setBusy(element, active) {
        element.classList.toggle("show", Boolean(active));
    },
    toast(element, text, timeout = 2800) {
        element.textContent = text;
        element.classList.add("show");
        window.clearTimeout(element._voteUiTimer);
        element._voteUiTimer = window.setTimeout(() => element.classList.remove("show"), timeout);
    },
    paged(element, rows, renderRows, options = {}) {
        const all = Array.isArray(rows) ? rows : [];
        const size = 20;
        let page = Math.max(1, Number(options.page || element._voteUiPage || 1));
        const pages = Math.max(1, Math.ceil(all.length / size));
        page = Math.min(page, pages);
        element._voteUiPage = page;
        element.innerHTML = renderRows(all.slice((page - 1) * size, page * size));
        const old = element.nextElementSibling;
        if (old?.classList.contains("vote-pager")) old.remove();
        if (all.length <= size) return {page, page_size: size, total: all.length, total_pages: pages};
        const pager = document.createElement("nav");
        pager.className = "vote-pager";
        pager.setAttribute("aria-label", "分頁");
        pager.innerHTML = `<button type="button" data-page="prev" ${page === 1 ? "disabled" : ""}>上一頁</button><span>第 ${page} / ${pages} 頁（共 ${all.length} 筆）</span><button type="button" data-page="next" ${page === pages ? "disabled" : ""}>下一頁</button>`;
        pager.addEventListener("click", event => {
            const action = event.target.closest("button")?.dataset.page;
            if (!action) return;
            element._voteUiPage = page + (action === "next" ? 1 : -1);
            window.VoteUI.paged(element, all, renderRows, options);
        });
        element.insertAdjacentElement("afterend", pager);
        return {page, page_size: size, total: all.length, total_pages: pages};
    },
    resetPage(element) { if (element) element._voteUiPage = 1; },
    async serverPaged(element, url, renderItems, page = 1) {
        element._voteServerSpec = {url, renderItems, page};
        element._voteServerObserver?.disconnect();
        if (!element._voteServerObserver) {
            element._voteServerObserver = new MutationObserver(() => {
                if (element._voteServerRendering || element._voteServerReloadQueued) return;
                element._voteServerReloadQueued = true;
                setTimeout(() => {
                    element._voteServerReloadQueued = false;
                    const spec = element._voteServerSpec;
                    if (spec) window.VoteUI.serverPaged(element, spec.url, spec.renderItems, spec.page).catch(() => {});
                }, 0);
            });
        }
        const target = new URL(url, location.origin);
        target.searchParams.set("page", Math.max(1, Number(page || 1)));
        const response = await fetch(target, {credentials: "same-origin"});
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
        element._voteServerRendering = true;
        element.innerHTML = renderItems(data.items || [], data);
        queueMicrotask(() => { element._voteServerRendering = false; });
        const old = element.nextElementSibling;
        if (old?.classList.contains("vote-pager-server")) old.remove();
        if ((data.total_pages || 1) > 1) {
            const pager = document.createElement("nav");
            pager.className = "vote-pager vote-pager-server";
            pager.innerHTML = `<button type="button" data-page="prev" ${data.page <= 1 ? "disabled" : ""}>上一頁</button><span>第 ${data.page} / ${data.total_pages} 頁（共 ${data.total} 筆）</span><button type="button" data-page="next" ${data.page >= data.total_pages ? "disabled" : ""}>下一頁</button>`;
            pager.onclick = event => {
                const action = event.target.closest("button")?.dataset.page;
                if (action) window.VoteUI.serverPaged(element, url, renderItems, data.page + (action === "next" ? 1 : -1));
            };
            element.insertAdjacentElement("afterend", pager);
        }
        element._voteServerSpec.page = data.page;
        element._voteServerObserver.observe(element, {childList: true, subtree: true});
        return data;
    },
    autoPageTables(root = document) {
        root.querySelectorAll("table tbody").forEach(tbody => {
            const rows = [...tbody.children].filter(row => row.tagName === "TR");
            const table = tbody.closest("table");
            if (!table || table.dataset.noPagination === "true") return;
            let pager = table.parentElement?.nextElementSibling;
            if (!pager?.classList.contains("vote-pager-auto")) pager = null;
            if (rows.length <= 20) {
                rows.forEach(row => row.hidden = false);
                pager?.remove();
                table._voteUiPage = 1;
                return;
            }
            const pages = Math.ceil(rows.length / 20);
            const page = Math.min(Math.max(1, Number(table._voteUiPage || 1)), pages);
            table._voteUiPage = page;
            rows.forEach((row, index) => row.hidden = index < (page - 1) * 20 || index >= page * 20);
            if (!pager) {
                pager = document.createElement("nav");
                pager.className = "vote-pager vote-pager-auto";
                pager.setAttribute("aria-label", "表格分頁");
                table.parentElement.insertAdjacentElement("afterend", pager);
            }
            pager.innerHTML = `<button type="button" data-page="prev" ${page === 1 ? "disabled" : ""}>上一頁</button><span>第 ${page} / ${pages} 頁（共 ${rows.length} 筆）</span><button type="button" data-page="next" ${page === pages ? "disabled" : ""}>下一頁</button>`;
            pager.onclick = event => {
                const action = event.target.closest("button")?.dataset.page;
                if (!action) return;
                table._voteUiPage = page + (action === "next" ? 1 : -1);
                window.VoteUI.autoPageTables(root);
            };
        });
    },
});

document.addEventListener("DOMContentLoaded", () => {
    const guideCopy = {
        "/ai-fund": ["查看基金結餘、個人入數紀錄及 AI 用量估算。", "AI管理員可在管理員分頁確認入數及更新設定。"],
        "/lateness-fund": ["先選擇學年，再查看結餘、成員統計及紀錄。", "新增或刪除紀錄後，罰款次數及應繳金額會重新計算。"],
        "/dev-settings": ["此頁只供 Developer 管理帳戶、密碼、AI 設定及系統狀態。", "更改 production 設定前請確認影響範圍。"],
        "/bug-report": ["請填寫可重現步驟、預期結果及實際結果。", "如可行，附上裝置、瀏覽器及截圖，方便跟進。"],
        "/open-db": ["可搜尋及篩選公開辯題庫；表格每頁顯示 20 筆。", "分類及難度資料只供參考。"],
        "/recent-matches": ["所有內部委員可查閱；只有高級委員可新增及更新。", "新增比賽及首次確認賽果會向已訂閱委員發出 push。"],
        "/team-history": ["Timeline 及任期均以 9 月至翌年 8 月為一個學年。", "只有畢業會自動成為老鬼及高級委員；離隊不會。"],
        "/ghost-forum": ["只限任期標示為畢業的委員帳戶。", "主題可連結既有比賽及圖片；此區沒有分區或版主。"],
    };
    if (!document.querySelector("details.guide") && guideCopy[location.pathname]) {
        const guide = document.createElement("details");
        guide.className = "guide";
        guide.innerHTML = `<summary>ℹ️ 首次使用指南</summary><div class="sub">${guideCopy[location.pathname].map(item => `• ${item}`).join("<br>")}</div>`;
        const heading = document.querySelector("main h1");
        const anchor = heading?.nextElementSibling?.matches(".caption,.intro,p") ? heading.nextElementSibling : heading;
        anchor?.insertAdjacentElement("afterend", guide);
    }
    document.querySelectorAll("details.guide > ul").forEach(list => {
        const content = document.createElement("div");
        content.className = "sub";
        [...list.children].forEach((item, index, items) => {
            content.append(`• ${item.textContent.trim()}`);
            if (index < items.length - 1) content.append(document.createElement("br"));
        });
        list.replaceWith(content);
    });
    let queued = false;
    const refresh = () => {
        if (queued) return;
        queued = true;
        requestAnimationFrame(() => { queued = false; window.VoteUI.autoPageTables(); });
    };
    refresh();
    new MutationObserver(mutations => {
        if (mutations.every(item => item.target.closest?.(".vote-pager"))) return;
        mutations.forEach(item => {
            const table = item.target.closest?.("tbody")?.closest("table");
            if (table) table._voteUiPage = 1;
        });
        refresh();
    }).observe(document.body, {childList: true, subtree: true});
});
