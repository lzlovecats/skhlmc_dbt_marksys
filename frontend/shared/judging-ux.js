(() => {
    "use strict";

    const $ = id => document.getElementById(id);
    const dirtySides = new Set();
    const nativeFetch = window.fetch.bind(window);
    let manualSaveSide = "";
    let judgeNameTimer = null;

    function panelForSide(side) {
        return side === "正方" ? $("panelPro") : $("panelCon");
    }

    function setDirty(side, dirty) {
        if (dirty) dirtySides.add(side);
        else dirtySides.delete(side);
        const panel = panelForSide(side);
        if (panel) panel.dataset.dirty = dirty ? "1" : "0";
    }

    window.fetch = async (input, options = {}) => {
        const url = typeof input === "string" ? input : input?.url || "";
        const isDraft = url === "/api/judging/draft" && String(options.method || "GET").toUpperCase() === "POST";
        if (!isDraft) return nativeFetch(input, options);
        let side = "";
        let requestJudge = "";
        try {
            const payload = JSON.parse(options.body || "{}");
            side = payload.side || "";
            requestJudge = String(payload.judge_name || "").trim();
        } catch {}
        const manual = manualSaveSide === side;
        if (!manual && !dirtySides.has(side)) {
            return Promise.reject(new Error(`略過未修改的${side || "一方"}自動暫存。`));
        }
        try {
            const response = await nativeFetch(input, options);
            const currentJudge = String($("judge")?.value || "").trim();
            if (response.ok && requestJudge === currentJudge) setDirty(side, false);
            return response;
        } finally {
            if (manual) manualSaveSide = "";
        }
    };

    function updateCompletionHint() {
        const hint = $("completionHint");
        const submit = $("submit");
        if (!hint || !submit) return;
        const ready = !submit.disabled;
        hint.className = ready ? "notice ok" : "caption";
        hint.textContent = ready
            ? "雙方評分已完成，確認無誤後可正式提交。"
            : "請先分別完成正方及反方評分，並各自暫存。";
        if ($("submitBottom")) $("submitBottom").disabled = submit.disabled;
    }

    function updateMatchAvailability() {
        const select = $("match");
        if (!select) return;
        const options = Array.from(select.options);
        options.forEach(option => {
            if (option.disabled) option.disabled = false;
        });
        const selected = options.find(option => option.value === select.value);
        const isOpen = !!selected && !selected.textContent.includes("未開放");
        const closed = $("closed");
        const password = $("password");
        const submit = $("loginSubmit");
        if (closed) {
            closed.classList.toggle("hidden", isOpen);
            closed.textContent = options.length
                ? "該場次未開放評分，請向賽會人員查詢。"
                : "目前未有比賽場次資料。請先由賽會人員建立場次。";
        }
        if (password) password.disabled = !isOpen;
        if (submit) submit.disabled = !isOpen;
    }

    function askLeave(title, text, confirmLabel) {
        return new Promise(resolve => {
            $("leaveTitle").textContent = title;
            $("leaveText").textContent = text;
            $("leaveYes").textContent = confirmLabel;
            $("leaveConfirm").classList.add("show");
            const finish = answer => {
                $("leaveConfirm").classList.remove("show");
                $("leaveYes").onclick = null;
                $("leaveNo").onclick = null;
                resolve(answer);
            };
            $("leaveYes").onclick = () => finish(true);
            $("leaveNo").onclick = () => finish(false);
        });
    }

    async function leaveJudging() {
        await fetch("/api/judging/logout", {
            method: "POST",
            credentials: "same-origin",
            headers: { "Content-Type": "application/json" },
            body: "{}",
        });
        location.reload();
    }

    $("submitBottom").onclick = () => $("submit").click();
    $("logout").onclick = async () => {
        if (await askLeave(
            "確認登出",
            "登出後，本機未提交的評分進度將會清除，請確保已儲存至雲端。",
            "確認登出",
        )) await leaveJudging();
    };
    $("switchMatch").onclick = async () => {
        if (await askLeave(
            "確認切換場次",
            "切換場次後，目前未提交的評分進度將會清除。請確保已暫存至雲端。",
            "確認切換",
        )) await leaveJudging();
    };

    $("saveRank").addEventListener("click", event => {
        const ranks = Array.from(document.querySelectorAll("[data-rank]"), el => Number(el.value));
        const valid = ranks.length === 8 && [...ranks].sort((a, b) => a - b).every((rank, index) => rank === index + 1);
        if (valid) return;
        event.preventDefault();
        event.stopImmediatePropagation();
        $("rankMsg").innerHTML = '<div class="notice warn">每個名次（1–8）必須恰好使用一次，請檢查是否有重複或遺漏。</div>';
    }, true);

    document.addEventListener("input", event => {
        if (!event.target.matches("#panelPro input[type=number], #panelCon input[type=number]")) return;
        setDirty(event.target.closest("#panelPro") ? "正方" : "反方", true);
    }, true);
    document.addEventListener("click", event => {
        const button = event.target.closest("[data-save]");
        if (button) manualSaveSide = button.dataset.save || "";
    }, true);

    $("judge").addEventListener("change", () => {
        clearTimeout(judgeNameTimer);
        judgeNameTimer = null;
        dirtySides.clear();
        manualSaveSide = "";
        $("scoreApp").classList.remove("hidden");
        $("status").innerHTML = "";
        $("panelPro").innerHTML = '<div class="notice">正在檢查此評判的雲端暫存紀錄…</div>';
        $("panelCon").innerHTML = "";
        $("submit").disabled = true;
        $("submitBottom").disabled = true;
        $("submitMsg").innerHTML = "";
    }, true);
    $("judge").addEventListener("input", () => {
        clearTimeout(judgeNameTimer);
        judgeNameTimer = setTimeout(() => {
            $("judge").dispatchEvent(new Event("change", { bubbles: true }));
        }, 450);
    });
    $("match").addEventListener("change", updateMatchAvailability);

    new MutationObserver(updateMatchAvailability).observe($("match"), { childList: true });
    new MutationObserver(updateCompletionHint).observe($("submit"), {
        attributes: true,
        attributeFilter: ["disabled"],
    });
    updateMatchAvailability();
    updateCompletionHint();
})();
