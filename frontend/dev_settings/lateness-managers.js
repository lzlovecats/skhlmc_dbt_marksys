(() => {
    const form = document.getElementById("trainingForm");
    const treasurers = document.getElementById("treasurers");
    if (!form || !treasurers) return;
    const label = document.createElement("label");
    label.innerHTML = '遲到基金管理員（每行一個帳戶）<textarea id="latenessManagers"></textarea>';
    treasurers.closest("label").insertAdjacentElement("afterend", label);
    const lines = value => String(value || "").split("\n").map(item => item.trim()).filter(Boolean);
    const load = async () => {
        if (document.getElementById("app").classList.contains("hidden")) return;
        const response = await fetch("/api/developer/data", {credentials: "same-origin"});
        if (!response.ok) return;
        const data = await response.json();
        try {
            const managers = JSON.parse(data.configs?.lateness_fund_managers || '["leungph"]');
            document.getElementById("latenessManagers").value = Array.isArray(managers) ? managers.join("\n") : "leungph";
        } catch { document.getElementById("latenessManagers").value = "leungph"; }
    };
    new MutationObserver(load).observe(document.getElementById("app"), {attributes: true, attributeFilter: ["class"]});
    form.addEventListener("submit", async event => {
        event.preventDefault(); event.stopImmediatePropagation();
        const values = {
            tts_recording_allowed_users: lines(document.getElementById("ttsAllowed").value),
            tts_recording_reviewers: lines(document.getElementById("ttsReviewers").value),
            ai_fund_treasurers: lines(document.getElementById("treasurers").value),
            lateness_fund_managers: lines(document.getElementById("latenessManagers").value),
        };
        if (!values.lateness_fund_managers.length) return VoteUI.toast(document.getElementById("toast"), "⚠️ 至少保留一位遲到基金管理員。");
        const response = await fetch("/api/developer/settings", {method: "POST", credentials: "same-origin", headers: {"Content-Type": "application/json"}, body: JSON.stringify({values})});
        const data = await response.json().catch(() => ({}));
        VoteUI.toast(document.getElementById("toast"), response.ok ? "✅ 管理員名單已更新。" : `⚠️ ${data.detail || "更新失敗。"}`);
    }, true);
    load();
})();
