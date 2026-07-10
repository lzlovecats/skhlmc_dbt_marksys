## 聖呂中辯自家聲線：Base Model 選型 + 讀音字典接線技術對比

> 補充 `tts_rd_plan.md`「三、單一主聲線」。目標：喺已收集嘅單人錄音上，揀一個 open-source 粵語 TTS base model 做 fine-tune / voice clone，並決定**現有 `tts_lexicon` 讀音字典點樣接落去**，令「似人聲」同「讀音準」兩件事都掂。
>
> 對照上一輪結論：**fine-tune 解決音色（似人聲），讀音準靠 G2P 前端 + 字典。** 所以選型唔止睇 clone 質素，仲要睇「隻模型畀唔畀你插手音素／接字典」。

---

### 〇、現況（決定咗點接字典）

| 位置 | 現況 | 對選型嘅影響 |
|---|---|---|
| `tts_lexicon` schema | `term`、`reading`（漢字諧音，runtime 用）、`jyutping`（收咗但**未用**）、`example`、`note`、`category` | `jyutping` 欄已存在，接音素層唔使改 schema／UI |
| `deploy/proxy.py` `_preprocess_tts_text` | 合成前 `term → reading` 字串 replace（長詞優先、60s cache），單人＋聯機共用 | 純文字覆寫，**provider-agnostic**；任何模型都食得，但靠模型自己 G2P |
| `_synthesize_tts` provider 層 | `TTS_PROVIDER=azure\|custom` 已就位 | 接 self-hosted 模型＝實作 `_synthesize_custom` 指去 GPU endpoint，主流程唔使動 |

**含意**：而家個字典係「文字層覆寫」。要「讀音硬性可控」（尤其破音字），中期要喺 proxy 加一層 G2P，令 `jyutping` 欄真正用得著（見第三節 T1）。

---

### 一、選型準則（依重要性排）

1. **粵語支援**：官方 or 成熟社群支援 `yue`，唔係硬砌普通話模型。
2. **少量資料出到音色**：你目標 30–60 分鐘單人料，要 few-shot / 細 fine-tune 就有相似度。
3. **讀音可控性**：畀唔畀你餵音素／jyutping，or 至少改到佢個 G2P 前端 —— 決定字典接得幾硬。
4. **License**：校用 / 可自部署，避免商用限制。
5. **部署成本**：GPU 需求、推理速度（單人合成同聯機即時廣播都要頂得順）。

---

### 二、候選 Base Model 對比

| 模型 | 粵語 | 輸入型態 | 少量料 clone | 讀音可控（接字典） | License | 部署 | 一句總結 |
|---|---|---|---|---|---|---|---|
| **GPT-SoVITS**（粵語版） | ✅ 官方 | 文字→內建粵語 G2P→音素 | ⭐⭐⭐ 1–5 分鐘已有相似度 | ⭐⭐ 可到音素層、可注入 jyutping | MIT | GPU，中 | **首選 v0**：few-shot 相似度高＋粵語＋摸到音素層 |
| **CosyVoice 2**（Alibaba） | ✅ `yue` | 文字 / 音素 token | ⭐⭐⭐ zero-shot + SFT | ⭐⭐ 音素 token 可控 | Apache-2.0 | GPU，較重 | 自然度／穩定度最好，適合中期規模化 |
| **Fish-Speech / OpenAudio** | ✅ 多語含粵 | 文字（內建 G2P） | ⭐⭐⭐ zero-shot + fine-tune | ⭐ 主要文字層，音素較封閉 | source-available（有商用限制，校內自用 OK） | GPU，中 | 質素高但字典只接得到文字層 |
| **F5-TTS** | 🟡 社群 | 文字（flow-matching） | ⭐⭐⭐ 10–30s 參考 | ⭐ 文字層為主 | MIT | GPU，中 | clone 快，但粵語＋讀音控制較弱 |
| **MeloTTS**（`yue`） | ✅ 官方 | **音素（VITS）** | ⭐ 需較多料訓單人 | ⭐⭐⭐ 原生食音素，jyutping 直插 | MIT | **CPU 都行**，輕 | 讀音最可控，但音色 clone 較弱；適合做「讀音硬控」fallback |
| **Piper**（espeak-ng `yue`） | 🟡 espeak 質素一般 | 音素（espeak） | ⭐ 要 per-voice 訓 | ⭐⭐⭐ 音素原生 | MIT | CPU，最輕 | 最省資源，但自然度偏機械 |
| **自訓 VITS / Matcha** | 由你 G2P 決定 | **jyutping 直餵** | ⭐ 要幾個鐘＋訓練功夫 | ⭐⭐⭐⭐ 完全掌控 | — | GPU＋工程 | 讀音控制天花板最高，但最重、最遲 |
| XTTS-v2（Coqui） | ❌ 無官方粵語 | 文字 | ⭐⭐ | ⭐ | Coqui 已停 | GPU | ❌ 唔建議：無粵語＋專案已死 |

> ⭐ 越多越好。「讀音可控」睇嘅係：字典覆寫接得到文字層（⭐）、音素／jyutping 層（⭐⭐⭐）、定完全自控（⭐⭐⭐⭐）。

**張力一句講清**：clone 最強嗰批（GPT-SoVITS / CosyVoice / Fish-Speech）食文字、G2P 收埋喺模型內；音素原生嗰批（MeloTTS / Piper / 自訓）字典插得最靚但 clone 較弱。所以要兩者夾。

---

### 三、讀音字典點接（三個 tier，逐級加硬）

**T0 — 文字層覆寫（＝現況，零改動）**
- 沿用 `_preprocess_tts_text` 嘅 `term → reading` 漢字覆寫，覆寫完先餵模型。
- 任何模型即插即用；`TTS_PROVIDER=custom` 換 endpoint 就得。
- 缺點：破音字最終讀法仲係模型自己 G2P 話事，你嘅覆寫未必食。
- 用途：v0 開機、先聽音色。

**T1 — jyutping 層覆寫（推薦，中期）** ← `jyutping` 欄終於用得著
1. proxy 加 G2P 層：`text → ToJyutping/PyCantonese → 逐字 jyutping`。
2. 用 `tts_lexicon.jyutping` 覆寫指定詞（人名、校名、術語、破音字）嘅 jyutping。
3. 把 jyutping 序列餵俾**支援音素輸入**嘅模型（MeloTTS 直食；GPT-SoVITS / CosyVoice2 可從音素層注入）。
- 好處：讀音變硬性可控，字典管理 UI（已有 jyutping 欄）唔使改、schema 唔使改。
- 成本：多一層 G2P + 模型走音素輸入路徑。

**T2 — SSML `<phoneme>` / Azure lexicon.xml（只限雲端 provider）**
- 留返俾 Azure fallback 用，同 open-source 模型無關，列出嚟做對照。

> 建議路線：**v0 用 T0 聽音色 → 一發現破音字覆寫唔食，就升 T1**。字典內容（term/reading/jyutping）而家就可以照填，T0、T1 都用得著，唔會白做。

---

### 四、建議

- **v0（即刻可做）**：**GPT-SoVITS 粵語版**，用你 accepted 錄音做 few-shot clone。字典行 **T0**。先確認音色似唔似本人。
- **v1（音色 OK 後）**：升 **T1 jyutping 覆寫**（proxy 加 ToJyutping 層），開始逐個破音字入字典校正。
- **中期（要規模化 / 更自然）**：評估 **CosyVoice 2** 做 base，音素層接 T1。
- **讀音硬控 fallback**：留 **MeloTTS-yue** 做「一定要讀啱」嘅句子（例如校名、比賽名）嘅備援管道。
- ❌ 唔好行 XTTS-v2（無粵語、專案已停）。

---

### 五、接落現有 code 嘅位

| 步驟 | 改邊度 |
|---|---|
| 接 self-hosted 模型 | `deploy/proxy.py` 實作 `_synthesize_custom`，`TTS_PROVIDER=custom` 指去 GPU endpoint |
| T1 G2P 層 | `_preprocess_tts_text` 之後加 `text → jyutping`，再套 `tts_lexicon.jyutping` 覆寫 |
| 字典內容 | `ai_training.py`「📖 讀音字典」tab（已有 term/reading/jyutping 欄），即刻可填 |
| 錄音資料 | 管理員 tab → 錄音審核 / Export → accepted dataset zip，做 clone 訓練集 |
