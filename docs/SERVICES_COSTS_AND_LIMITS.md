# 外部服務成本速查

最後核實：2026-07-17（Asia/Hong_Kong）

本文件只供維護者估算外部服務成本。金額均為美元、未連稅項；實際收費以 provider
dashboard、合約及官方 pricing page 為準。為免匯率及模型價格過期，不保存港元換算或
逐個 AI model 的價格 snapshot。

| 服務 | 現行成本基線／免費額 | 超額或可變成本 | 官方價格 |
|---|---|---|---|
| Render | Starter web service：US$7／月；Hobby workspace：US$0／月 | Hobby 每月包 5GB bandwidth，其後 US$0.15／GB；如升 Pro workspace 另加 US$25／月 | [Render pricing](https://render.com/pricing/) |
| Supabase | Free：US$0／月；每 project 包 500MB database、5GB egress、5GB cached egress、1GB storage | Pro 由 US$25／月；包 8GB disk、250GB egress、250GB cached egress、100GB storage，超額分別為 US$0.125／GB、US$0.09／GB、US$0.03／GB、US$0.0213／GB | [Supabase pricing](https://supabase.com/pricing) |
| Cloudflare R2 Standard | 每月免費 10GB-month、100萬次 Class A、1,000萬次 Class B；Internet egress 免費 | Storage US$0.015／GB-month；Class A US$4.50／100萬次；Class B US$0.36／100萬次 | [R2 pricing](https://developers.cloudflare.com/r2/pricing/) |
| Google Gemini Developer API | 無固定月費；支援免費額的模型可用 Free Tier | Paid Tier 按模型的 input、output、cache、audio、search 等實際用量收費；使用前查當時模型價格 | [Gemini API pricing](https://ai.google.dev/gemini-api/docs/pricing) |
| OpenRouter | 無固定月費或最低 inference spend；免費模型另有低 rate limit | 模型 inference 按 provider 公開價；購買 credits 收 5.5% platform fee，最低 US$0.80；現行單次購買最低 US$5 | [OpenRouter pricing](https://openrouter.ai/pricing), [billing FAQ](https://openrouter.ai/docs/faq) |
| Azure Speech TTS | Free F0：US$0；Neural TTS 每月包 50萬 characters | Pay-as-you-go 按合成 characters、region、currency 及 Azure offer 收費；付款前用 calculator 查實價 | [Azure Speech pricing](https://azure.microsoft.com/pricing/details/speech/) |
| YouTube embed | Repo 沒有 YouTube 付費 plan 或固定費用 | US$0 直接服務成本；影片 hosting／帳戶成本由影片擁有人承擔 | — |
| Web Push／VAPID | Repo 沒有第三方 push 付費 plan | US$0 直接服務成本 | — |

## 現行固定基線

在 Supabase 保持 Free、R2 不超免費額，而且 AI／TTS provider 沒有付費用量時，已知固定
成本為 **US$7／月**（Render Starter）。AI、語音、儲存及超額流量全部另按實際用量計算。

每次改 plan、付款或編預算前，必須重新開啟上表官方連結核對，並更新「最後核實」日期。
