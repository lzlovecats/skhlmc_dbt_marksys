# Release notes

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
