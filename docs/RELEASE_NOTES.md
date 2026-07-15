# Release notes

## 4.5.1 — Video replay best-debater multi-select

- 比賽片段重溫及管理頁的章節設定改為逐個個人發言辯位勾選「佳辯」，同一片段可標示多位最佳辯論員。
- 佳辯章節按鈕會以「正主（佳辯）」格式顯示；舊版單選API欄位暫時保留作向後相容。
- 套用前必須先執行`20260715_0002_allow_multiple_video_best_debaters` migration，移除每條片段只可一位佳辯的唯一索引。

## 4.5.0 — AI resource and network-practice breaking change

- AI／media每日、每週及每月使用次數quota，以及Developer Solo exemption已移除；
  API只再回傳Render、R2及provider system-wide限制。
- AI Coach錄音分析已改用browser直傳R2及Google Files API；舊`audio_base64`
  request會回410或validation error，client必須同版本更新。
- 聯機房只接受兩位真人Mode A；`mode=B`明確回400，舊room audio／Gemini／TTS
  WebSocket messages已移除，不設TURN、SFU或Render media fallback。
- Mode A新增`rtc_offer`、`rtc_answer`、`rtc_ice`、`rtc_status`、
  `transcript_chunk`、`transcript_commit`及`judge_disabled`控制訊息。
- 套用前必須先執行`20260715_0001_monthly_resource_limits_and_remove_quotas` migration，
  並同一次release更新server及browser assets。
