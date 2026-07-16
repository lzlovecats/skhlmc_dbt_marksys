# AI 使用位置與模型清單

最後更新：4.4.0

所有可執行程式的模型選擇集中在根目錄 `ai_model_config.py`。API、core
logic 或 `deploy/proxy.py` 不應自行寫 provider model slug；顯示文字、測試假
model 及外部訓練產物 ID 不屬於執行時模型選擇。開發者設定內的
`ai_enabled_providers` 與 `ai_default_model` 會即時限制 AI Coach 顯示及接受的
模型；未設定時由 `resolve_interactive_model_settings` 保留現行全部 provider
及 `DEFAULT_AI_MODEL` 行為。

## 線上功能

| 功能 | 實作位置 | Provider / 模型 | 集中設定 |
| --- | --- | --- | --- |
| AI Coach 發言評語、策略、搵料、Fact Check | `api/ai_coach_api.py`, `core/ai_provider.py` | 使用者可選 Gemini 2.5 Flash、Gemini 3.5 Flash、Gemini 3.1 Pro、DeepSeek V4 Pro、Haiku 4.5、GPT-5.4 Mini；另可選已通過 registry gate 的自家 OpenAI-compatible LLM | `AI_MODEL_OPTIONS`, `DEFAULT_AI_MODEL`, `CUSTOM_LLM_OPTION` |
| 投票頁辯題審查、討論區 @Gemini、辯題庫及往績分析 | `api/vote_api.py`, `core/vote_ai.py` | Gemini 3.5 Flash | `AI_FEATURE_MODEL_LABELS`: `vote_review`, `vote_discussion`, `vote_analysis` |
| Solo Free Debate / 完整 Mock 語音陪練 | `api/ai_coach_api.py`, `deploy/proxy.py`, `templates/live_debate.html` | Gemini Live `gemini-3.1-flash-live-preview` | `GEMINI_LIVE_MODEL`, `GEMINI_LIVE_MODEL_LABEL`, `GEMINI_LIVE_PROVIDER` |
| Mode A真人聯機完場AI評判 | `deploy/proxy.py`, `frontend/shared/room-debate-p2p.js` | 正反雙方各有Browser逐字稿時，依次 Gemini 3.5 Flash → Gemini 2.5 Flash → Gemini 2.5 Flash Lite；P2P音訊不送provider | `AI_FEATURE_MODEL_FALLBACK_LABELS["room_judgement"]` |
| AI評判易（正式比賽 Kiosk 全場逐字稿、原音交叉評語及建議勝方） | `api/kiosk_api.py`, `api/projector_ai_api.py`, `templates/projector_control.html`, `templates/projector_display.html` | 同一個 Gemini 3.5 Flash 連續兩次呼叫：先以原音做逐字稿及分隊，再以原音＋逐字稿＋正式場次環節記號評審（Gemini-only，必須支援 audio） | `AI_FEATURE_MODEL_LABELS["kiosk_match_review"]`, `AI_FEATURE_REQUIREMENTS` |
| AI Training 錄音音質檢查 | `api/ai_training_api.py` | Gemini 2.5 Flash（Gemini-only，必須支援 audio） | `AI_FEATURE_MODEL_LABELS["tts_review"]`, `AI_FEATURE_REQUIREMENTS` |
| AI Training 文字資料預檢 | `api/ai_training_api.py` | Gemini 2.5 Flash（Gemini-only） | `AI_FEATURE_MODEL_LABELS["llm_review"]`, `AI_FEATURE_REQUIREMENTS` |
| AI Training 句庫覆蓋分析及重整建議 | `api/ai_training_api.py` | Gemini 2.5 Flash（Gemini-only） | `AI_FEATURE_MODEL_LABELS["tts_script_analysis"]`, `AI_FEATURE_REQUIREMENTS` |
| RAG 查詢及重建向量 | `core/rag.py`, `api/ai_training_api.py` | Gemini Embedding 2；索引空間版本 `gemini-embedding-2@2026-04` | `RAG_EMBEDDING_MODEL`, `RAG_EMBEDDING_VERSION` |
| AI eval baseline 工作單 | `api/ai_training_api.py` | 預設標示 Gemini 2.5 Flash；HTTP API 只鎖定 case，由受控 worker 執行 | `AI_FEATURE_MODEL_LABELS["ai_training_eval"]` |

## 語音合成與離線訓練

| 功能 | 實作位置 | 模型／引擎管理方式 |
| --- | --- | --- |
| Azure TTS | `deploy/proxy.py` | 聲音由 `TTS_PROVIDER_OPTIONS["azure"]` 集中定義 selector 及預設 `zh-HK-HiuMaanNeural`；部署可用 `AZURE_TTS_VOICE` 覆寫。AI評判易只會合成最多 1,200 字的投影摘要；沒有可用 TTS provider 時只顯示文字 |
| 自家 TTS inference | `deploy/proxy.py` | `TTS_PROVIDER_OPTIONS["custom"]` 集中定義 endpoint、key、deployable model version selectors；實際 checkpoint 由 `CUSTOM_TTS_MODEL_VERSION` 指向 registry 內已通過 gate 的版本 |
| 廣東話聲音模型資料準備 | `tools/prepare_gpt_sovits_dataset.py` | 訓練引擎及工具 CLI / WebUI 指引名稱由 `LOCAL_TTS_TRAINING_ENGINE = "GPT-SoVITS"` 產生；實際 checkpoint / experiment ID 是訓練產物，不是 app 的固定推理選擇 |
| 自家辯論 LLM | `api/ai_coach_api.py`, AI model registry | label、capabilities、部署 selector 全在 `CUSTOM_LLM_OPTION`；實際 model ID 由 `CUSTOM_LLM_MODEL` 指向，且必須在 `ai_model_versions` 為 deployable |

## 非 AI 呼叫

- `Gemini` 投票留言作者是資料庫 pseudo-account，用來標示 AI 回覆，不是另一個模型。
- AI 基金頁只記錄／展示用量和成本，不自行呼叫模型。
- `assets/ai_eval_cases_v0.json` 是評估題庫，不自行呼叫模型。

## 更改模型

1. 一般 AI Coach 選項：改 `AI_MODEL_OPTIONS`。
2. 自動功能預設：改 `AI_FEATURE_MODEL_LABELS`。
3. 完場評判 fallback：改 `AI_FEATURE_MODEL_FALLBACK_LABELS`。
4. Live 或 embedding：改 `GEMINI_LIVE_MODEL`、`RAG_EMBEDDING_MODEL` 及相應版本。
5. 若 caller 綁定 Gemini API 或需要 audio 等能力，同步保留 `AI_FEATURE_REQUIREMENTS`；啟動及測試會拒絕 provider／能力不合資格的模型。
6. 自家 LLM 或 TTS／Azure voice selector：改 `CUSTOM_LLM_OPTION`、`TTS_PROVIDER_OPTIONS`；部署時只填這些集中設定列出的 secret key。
