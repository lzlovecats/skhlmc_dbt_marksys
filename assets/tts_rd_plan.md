## 聖呂中辯自家讀音模型 / 聲線 / 辯論 LLM 研發計劃書

> 目標：建立一套屬於聖呂中辯的廣東話語音及辯論 AI 系統。系統分三條工作線推進：**讀音層**負責讀音正確、**單一主聲線**負責自然穩定地朗讀、**辯論 LLM**負責辯論內容、攻防及評語。三者最後會在 AI 辯論易整合，但訓練和評估應分開處理。

---

### 〇、現況總覽（2026-07-09 更新）

用 ✅ 已完成 / 🟡 部分完成 / ⬜ 未開始 標示，方便對照現況：

| 工作線 | 狀態 | 備註 |
|---|---|---|
| 二、讀音層（字典部分） | 🟡 基建已完成 | 字典表 `tts_lexicon`、`ai_training.py`「📖 讀音字典」管理 tab、runtime 覆寫（`_preprocess_tts_text` 讀 active 字典 + 60s cache，單人+聯機共用）已落地。**尚欠**：實際填字典內容、（可選）G2P（ToJyutping/PyCantonese）自動初稿、測試集。 |
| 三、單一主聲線（錄音收集） | 🟡 收集基建已完成 | 同意書、句庫、錄音、AI 預檢、管理員審核、`accepted` dataset zip export、句庫缺口分析已喺 `ai_training.py` 落地；**尚欠**：實際收夠 30-60 分鐘、真正訓練 v0 checkpoint、A/B。 |
| 四、辯論 LLM（文字收集） | 🟡 收集基建已完成 | 文字提交、匿名化 AI 檢查、管理員審核、JSONL export 已落地；**尚欠**：固定 eval set、RAG 接線、（如需要）fine-tune。 |
| Azure TTS runtime（單人 + 聯機） | ✅ 已完成 | 單人 `/api/tts/azure`；聯機 Mode B 由 proxy `_room_gemini_pump` server 端合成、同步廣播全房，失敗 fallback Gemini 原生；provider 抽象層 `_synthesize_tts`（`TTS_PROVIDER=azure\|custom`）已就位。 |

> 已建基建位置：`ai_training.py`（TTS 錄音 tab、LLM 文字 tab、管理員審核 / export）、`schema.py`（`tts_voice_consents` / `tts_voice_recordings` / `tts_scripts` / `llm_training_submissions`）、`deploy/proxy.py`（`_synthesize_tts` / `_preprocess_tts_text` / 聯機合成）。

---

### 一、總原則

- **不由零開始 pre-train 大模型**：成本和資料量都不實際。採用現成模型、自家資料及小範圍 fine-tune / RAG。
- **TTS 與 LLM 分開研發**：TTS 處理「如何朗讀」，LLM 處理「應該說甚麼內容」。兩者可以同步進行，最後以 API 串接。
- **先建立可控版本**：先建立讀音字典和單一主聲線，穩定後才考慮多聲線。
- **私隱優先**：只使用已書面同意的錄音；同意可撤回；撤回後錄音不再 export，新模型重訓時必須排除相關資料。
- **資料質素優先於數量**：背景聲、爆咪、讀錯稿、口誤都會直接降低模型質素。

---

### 二、讀音層：G2P 前端 + 字典　🟡 基建已完成

讀音層目標是令系統讀音正確，尤其是香港粵語常見多音字、人名、辯論術語、英文縮寫、數字和日期。這一層不需要等待自家聲線完成，可以先在現有 Azure TTS 上落地。

> **現況（基建已落地）**：
> - **資料表** `tts_lexicon`（`schema.py`）：`term`（原文）、`reading`（合成前覆寫成嘅讀法/諧音，runtime 用）、`jyutping`（粵拼，備註/將來 SSML 用）、`example`、`note`、`category`、`is_active`。與句庫（`tts_scripts`＝錄音句子）分開。
> - **管理入口** `ai_training.py`「📖 讀音字典」tab：全體委員可查看生效字典；管理員可新增 / 編輯 / 停用（抄句庫管理 pattern）。
> - **Runtime** `deploy/proxy.py` `_preprocess_tts_text`：讀 active 字典（60 秒 TTL cache、DB 出錯保留上一份），合成前把 `term` → `reading` 覆寫（長詞優先）。單人 `/api/tts/azure` 同聯機 `_room_gemini_pump` 共用，**改一次兩邊生效**。
>
> **尚欠**：實際填字典內容、（可選）ToJyutping/PyCantonese 自動產生 `jyutping` 初稿、讀音測試集回歸。覆寫目前用字串 replace（provider-agnostic）；將來要更準可改用 SSML `<phoneme>` / Azure lexicon.xml。

#### 執行步驟（字典內容）

1. **建立讀音測試集**
   - 收集 100-200 句常用辯論句子。
   - 覆蓋人名、校名、比賽名、DSE / AI / GPT 等英文縮寫。
   - 覆蓋多音字，例如 `行`、`重`、`長`、`樂`、`分`。
   - 覆蓋百分比、年份、金額、分數、電話式數字、比賽時間。

2. **建立覆寫字典**
   - 每條記錄包含：原文、標準讀法、粵拼、例句、備註。
   - 優先處理辯論場景高頻詞，例如「基本法盃」、「自由辯論」、「主線」、「駁論」、「政策成本」。
   - 每次發現 TTS 讀錯字，應記錄為讀音問題，再補入字典。

3. **接入 G2P 工具**
   - 可先試用 `ToJyutping` / `PyCantonese`，用作初步文字轉粵拼。
   - G2P 結果先經覆寫字典修正，再交予 TTS。
   - 初期不必追求全自動完美；高頻詞以人手字典覆蓋最穩妥。

4. **先落 Azure Lexicon**
   - 產出 `lexicon.xml` 或等價設定，供現有 Azure TTS 使用。
   - AI 辯論易現有 Azure endpoint 保持不變，只在送出前做文字 / 讀音前處理。
   - 每次更新字典後，使用測試集跑一次讀音檢查。

#### 完成準則

- 測試集主要詞彙讀音正確率達標。
- 所有校隊常用辯論術語、比賽名、英文縮寫都能正確朗讀。
- 發現讀錯字時，有清楚流程補字典、重測、再部署。

---

### 三、單一主聲線：自家粵語 TTS v0　🟡 收集基建已完成

單一主聲線目標是先建立一把穩定、自然、授權清楚的自家聲線。不建議一開始將多位委員錄音混成一把「平均聲」，因為會令聲線不穩、授權難處理，撤回時亦難以清理。

> **現況**：以下 1-4、6 的**收集與部署基建已喺 `ai_training.py` 落地** —— 同意 / 撤回、句庫、錄音、AI 預檢、管理員審核（accepted / rejected / withdrawn）、`accepted` dataset zip export（`tts_voice_dataset.zip` + metadata）、按聲線 export，以及 runtime 的 provider 抽象層（第 6 步的 `/api/tts/custom` 只需填 `_synthesize_custom` + 設 `TTS_PROVIDER=custom`）。**尚欠純研發部分**：真正收夠 30-60 分鐘 accepted 錄音（第 2 步）、模型訓練（第 5 步）、與 Azure A/B（第 5 步）。

#### 執行步驟

1. **選定主聲線人選**
   - 先選 1 位願意長期授權、聲線清楚、錄音環境穩定的委員。
   - 同意書要清楚寫明：可用於 TTS 訓練、可生成近似本人聲音、只限聖呂中辯內部系統使用。
   - 如涉及未成年委員，應另行確認家長 / 學校層面的同意安排。

2. **收集錄音**
   - v0 原型：先收集 30-60 分鐘 accepted 錄音。
   - 正式版：目標為 1-3 小時高質錄音。
   - 每段 1-60 秒，單一講者、安靜、沒有爆咪、讀稿一致。
   - 句庫要覆蓋短句、長句、追問、反駁、評語、數字、英文術語、多音字。

3. **人工審核**
   - AI 預檢只作初步篩選，最後一定由管理員試聽。
   - `accepted` 才可以納入 dataset；`rejected` 要寫清原因；撤回同意後標記為 `withdrawn`。
   - export dataset 時只包含 `accepted`，並保留 `metadata.csv`。

4. **模型選型**
   - 快速原型：`GPT-SoVITS`，few-shot 快，適合先測試聲線相似度。
   - 較完整方案：`CosyVoice2` / 後續 CosyVoice 系列，較適合生產化和串流。
   - 每次選型前要檢查模型 license、預訓練權重條款、商用 / 內部使用限制。

5. **訓練與評估**
   - 使用 export 出來的 `dataset.zip` 作訓練資料。
   - 評估三項：讀音正確率、自然度、聲線一致性。
   - 用 ASR 計算 CER / WER 作粗略量化，再安排 3-5 人做 1-5 分盲聽 MOS。
   - 與 Azure TTS 做 A/B 對比，不應只憑主觀感覺決定上線。

6. **部署**
   - 將自家 TTS 包成 API，例如 `/api/tts/custom`。
   - 與現有 `/api/tts/azure` 並存，用設定開關切換。
   - 初期只開放予管理員或測試群組 A/B 試用；穩定後再開放予所有委員。

#### 完成準則

- 有 30-60 分鐘以上 `accepted` 主聲線資料。
- v0 checkpoint 可以穩定生成 5-10 句辯論句子。
- 延遲、自然度、讀音正確率達到內部可用水平。
- 任何已撤回錄音都不會再出現在下一次 export / 重訓資料中。

---

### 三之二、自家聲上線：latency 準則 + 分階段 rollout（非即時先行）　⬜ 未開始

> 決策背景：自家聲（GPT-SoVITS 等）唔係「本質慢」，而係**部署方式**決定快慢。現有 Render 512MB proxy **跑唔到**（要 GPU + 幾 GB VRAM），一定係獨立 GPU 推理服務。換走 Azure＝把「快同穩」由 managed API 接返自己孭。所以**即時路徑唔貿然換 Azure，自家聲先喺非即時場景落地**。`_synthesize_tts` 的 `TTS_PROVIDER=azure\|custom` 抽象層已容許分階段切換。

#### latency 準則（達標先可以接即時路徑）

- **硬件**：必須 GPU 且 model 常駐預熱（keep warm）；純 CPU 或每次冷啟動 load model 一律當唔合格。
- **分句 streaming**：邊合成邊播第一句，唔好等成段合成完先出聲。
- **快取**：高頻句／固定開場白／評語模板做 cache。
- **量度 end-to-end**（合成＋網絡＋廣播，唔淨係推理）：
  - 單人 `/api/tts/azure`：first-audio 目標 **< ~1 秒**。
  - 聯機 Mode B（`_room_gemini_pump` server 端合成再廣播全房）：合成慢＝**成房一齊等**，體感放大，門檻要更嚴。
- **穩定度**：GPU endpoint 要有 uptime / fallback；自家聲掛咗要自動 fallback 返 Azure（沿用現有 fallback 機制）。

#### 分階段 rollout

| 階段 | 用喺邊 | Provider |
|---|---|---|
| P0 試音 | 管理員 A/B 試聽、離線生成 | `custom`（唔趕時間，慢啲無所謂） |
| P1 非即時 | 預先生成內容、示範音、固定音檔 | `custom` |
| P2 即時（達標後） | 單人 Free De/Mock | `custom`，達 latency 準則先切；未達留 `azure` |
| P3 即時廣播（最後） | 聯機 Mode B 多人房 | `custom`，門檻最嚴，最後先接 |

> 原則：**Azure 一直係即時路徑嘅預設同 fallback**；自家聲由「唔趕時間」嗰端逐格向即時推進，每格都要先量到 latency 達標先升級。

---

### 三之三、硬件策略：訓練租 CUDA / 推理視乎是否本地　⬜ 未開始

> 涵蓋 TTS（讀音／聲線）同辯論 LLM 兩條線嘅硬件決定。核心觀察：**兩條線嘅「訓練」都係 CUDA 優先，Mac（Apple Silicon）強項係「推理 / serving」唔係「訓練」。** 所以「一部機打天下」唔成立，要按角色分。

| 角色 | 建議硬件 | 原因 |
|---|---|---|
| **訓練 / fine-tune**（TTS GPT-SoVITS + LLM LoRA） | **租雲 CUDA**（按鐘） | CUDA-first 生態（Unsloth / bitsandbytes / axolotl）齊；訓練 bursty 且罕有（料儲夠先訓一次），租最抵、零 capex |
| **LLM 推理 / serving / RAG dev** | 可考慮 **Mac mini（M4 Pro，≥48–64GB unified）** | Apple Silicon unified memory 係真着數，跑到同價 NVIDIA 消費卡跑唔起嘅大 quant model；靜、慳電、24/7；MLX 生態成熟 |
| **TTS live serving** | **CUDA endpoint** | 見三之二；MPS 推理慢，即時路徑唔頂 |

- **原則**：唔好為「訓練」買硬件 —— 訓練一律租 CUDA。買 Mac mini 嘅唯一合理理由係「本地 LLM 推理 + serving + dev」，唔係攞�嚟 tune。
- **Mac mini 對 GPT-SoVITS（TTS）唔夾**：CUDA-first，MPS 部分算子 fallback CPU，訓練尤其痛。即使買咗 Mac，TTS 訓練都要租 CUDA。
- **LLM 是否需要本地機**：視乎辯論 LLM 之後行雲 API 定 self-host 推理。若行雲 API，連 Mac mini 都未必需要；若要慳 API 錢做本地推理，Mac mini 先有位。
- **決策次序**：先確認 LLM 用雲 API 定本地 self-host → 若本地，再評估 Mac mini vs 專用 NVIDIA box（睇需唔需要同時做 TTS 推理，需要就 NVIDIA 贏）。

---

### 四、辯論 LLM：內容、策略、攻防及評語　🟡 收集基建已完成

辯論 LLM 目標是令 AI 更接近校隊教練：理解香港中學辯論語境、評分標準、追問方式和主線漏洞。這一條工作線不需要用聲音錄音直接訓練，主要使用文字資料和檢索。

> **現況**：**文字收集基建已喺 `ai_training.py` 落地** —— 委員提交（發言稿 / 逐字稿 / 評語 / 攻防 / 主線 / 辯題）、匿名化 + 有權使用 AI 檢查、管理員審核、`accepted` JSONL export（`llm_training_submissions` 表）。**尚欠**：① 第 4 步「固定 eval set（20-50 條）」目前**未有實作**，係最需要補嘅盲點，冇佢無法客觀比較 prompt / RAG / 模型改動；② 第 2 步 RAG 索引與接線；③ export 出嘅原始文字要再轉成 instruction / chat pairs 先可 fine-tune（第 5 步）。**建議次序：先 RAG → 砌 eval set → 唔夠先 LoRA。**

#### fine-tune 前具體清單（承第 5 步）

若最終要 fine-tune 自家 LLM，除現有 JSONL export 外仲需要：
- **資料格式**：把原始提交轉成高質 instruction / response（或 chat）pairs，唔係直接擺逐字稿。
- **base model + license**：揀粵語能力好的 open weights（如 Qwen2.5 系 / Yi / DeepSeek open），核對商用 / 內部使用條款。
- **eval set**：即上面 ① 的固定評估題，改任何嘢都用同一批比較。
- **compute**：LoRA / QLoRA 通常要租 cloud GPU —— 同意書須列明是否使用雲端 GPU（見第六節）。
- **serving + 接線**：部署（如 vLLM）後接入 app 現有 provider / 模型切換設定。

LLM 文字資料可以由不同委員提交，包括發言稿、逐字稿、評語、攻防問答、主線策略和辯題資料；但所有內容必須有權使用、已匿名化，並經管理員審核後才可以 export 入 dataset。

#### 執行步驟

1. **整理文字知識庫**
   - 比賽規則、評分準則、校隊用語、常見辯題、過往評語、優秀稿件。
   - 如有 Free De / Mock 逐字稿，先匿名化，再標註正反方、環節、辯題、評語。
   - 避免直接放入敏感個人資料、未授權學生資料、私人對話。

2. **先做 RAG，不急於 fine-tune**
   - 建立文件索引，按辯題、規則、評分標準、歷史例子檢索。
   - 生成回答時先檢索相關資料，再由 Gemini / GPT / DeepSeek 生成。
   - 好處是速度快、可更新，資料出錯時亦容易刪改。

3. **建立辯論任務模板**
   - 發言檢查：按內容、辭鋒、組織、風度評分。
   - 主線策劃：輸入辯題，產出正反主線、定義、標準、例子。
   - Free De 對練：AI 扮演對方，即時追問和反駁。
   - Mock 評語：按環節總結強弱，引用用戶實際發言。

4. **建立評估集**
   - 固定 20-50 條辯題 / 發言樣本。
   - 每次改 prompt、改 RAG、改模型，都用同一批樣本比較。
   - 評估準則包括：是否粵語自然、是否引用準確、是否有具體改善建議、是否避免空泛評語。

5. **之後才考慮 fine-tune / LoRA**
   - 當 RAG + prompt 已經穩定，但仍然有固定風格不足，才考慮 fine-tune。
   - fine-tune 資料應該是高質問答對、評語樣本、攻防樣本，而不是原始錄音。
   - 自家 LLM 可用開源模型做 LoRA，但部署成本、速度、中文 / 粵語能力要先測試。

#### 完成準則

- AI 回覆能穩定使用自然香港粵語，並保留正式辯論術語。
- 評語能具體引用用戶內容，而不是空泛鼓勵。
- 對同一批評估題，輸出品質比現有 prompt-only 版本明顯穩定。

---

### 五、TTS 與辯論 LLM 可否同步進行？

可以，而且建議同步進行，但要分工清楚：

| 工作線 | 可同步做？ | 依賴 |
|---|---:|---|
| 讀音層 | 可以即時開始 | 字典、測試集 |
| 單一主聲線 | 可以即時開始收音 | 同意、句庫、審核 |
| 辯論 LLM / RAG | 可以即時開始 | 文字資料、評估集 |
| 自家 TTS API | 要等待 v0 checkpoint | 訓練完成 |
| 完整語音辯論體驗 | 要等待 TTS API + LLM 流程穩定 | 兩條工作線整合 |

實際安排：

- W1-W2：讀音字典、測試集、RAG 文件整理、繼續收集主聲線錄音。
- W3-W4：Azure 讀音層上線；主聲線收集 30-60 分鐘；RAG v0 接入發言檢查 / 主線策劃。
- W5-W7：訓練自家 TTS v0；辯論 LLM 建立固定評估集；開始 A/B 比較。
- W8-W10：`/api/tts/custom` 串接；Free De / Mock 可切換自家 TTS；決定是否擴展第二把聲線。

> 註：runtime 側的 Azure TTS（單人 + 聯機）與 provider 抽象層已提前完成，故 W8-W10 的「串接」實際只剩填 `_synthesize_custom` + 切 `TTS_PROVIDER`；讀音層（W1-W4）先做，收益即時反映到現有 Azure 播放。

---

### 五之二、資料 → runtime 生效（資料流）

委員喺 `ai_training.py` 管理的資料，點樣真正影響到辯論易播出來的聲：

- **讀音字典**：`ai_training.py` 字典 tab → `tts_lexicon`（DB）→ proxy `_preprocess_tts_text` 讀 active 字典（帶短 TTL cache）→ 覆寫後交 `_synthesize_tts` → Azure / 自家模型。**單人（`/api/tts/azure`）同聯機（`_room_gemini_pump`）共用同一條路，改一次兩邊生效。**
- **主聲線錄音**：字典 tab 以外的錄音 tab → `tts_voice_recordings`（`accepted`）→ export `tts_voice_dataset.zip` → 離線訓練自家 TTS → 部署成 `CUSTOM_TTS_URL` → `_synthesize_custom` → 設 `TTS_PROVIDER=custom` 全域切換。
- **LLM 文字**：LLM tab → `llm_training_submissions`（`accepted`）→ JSONL export → RAG 索引 / fine-tune → 接入 app provider 設定。
- **共通原則**：訓練 / 重訓一律只食 `accepted`；`withdrawn` 不再 export，重訓必須排除（見第六節）。

---

### 六、私隱與授權要求

- 錄音只用於聖呂中辯內部 TTS、讀音檢查及相關 AI 研究測試。
- 未經額外同意，不公開錄音、不公開 checkpoint、不提供予第三方使用。
- 同意書要清楚列明用途、保存方式、撤回方法、是否會使用雲端 GPU。
- 撤回後：既有錄音標記為 `withdrawn`，不再 export；下一次重訓不得包含該錄音。
- 如模型已經使用撤回者聲音訓練，應停止使用該 checkpoint，並用排除後資料重訓。
