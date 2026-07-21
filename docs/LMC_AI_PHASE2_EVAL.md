# 自家 AI Phase 2：固定三模式盲評

Phase 2 以同一套30條固定題目，比較4B日常、4B Thinking及9B Thinking。每題三個模式各固定生成一次答案，再比較三組pair；全程只經目前選用的本地AI node，不使用Gemini、cloud fallback、RAG、web或舊對話。

## 會產生甚麼資料

每個完整campaign有：

- 90個固定答案（30題 × 3模式）；
- 90個blind case-pair（每題3組比較）；
- 270張完整票（每個pair由3名不同委員評核）；
- 270個整體偏好標籤；
- 最多1,350個分項偏好標籤，分別反映香港粵語自然度、論證／推理、具體實用、事實可靠及私隱安全。

所有答案都保存suite、prompt、persona、model tag、exact digest、runtime及generation fingerprint。Reviewer身份只在private database用作防重、quorum及audit，唔會出現在會員畫面、aggregate或manager export。系統不保存Thinking trace、hidden reasoning或對話歷史。

## 對自家 AI 發展的幫助

模式選擇及問題定位屬高價值。Summary可以直接回答Thinking相對日常模式有冇穩定增益，以及9B增益是否值得額外延遲及VRAM；結果亦會按speech review、strategy、attack/defence、mock judgement及粵語風格分拆，協助判斷問題來自模型大小、Thinking、persona還是prompt。

更新回歸亦屬高價值。日後更換model digest、Ollama版本、persona或prompt時，必須建立新campaign；舊campaign保持immutable，因此可用相同suite比較版本，避免只靠個別試用印象。

Phase 3準備屬高價值。Closed campaign嘅summary hash同provenance可成為model registry的正式eval evidence；`both_bad`、安全雙失敗及低分題型可成為RAG／SFT data factory的優先補強清單。不過Phase 2不會自動把答案或投票加入dataset，亦不會自動升級模型、改production default或啟用RAG。

直接訓練價值只屬低至中。90個答案及270票足夠作內部方向判斷，但不足以直接做可靠preference training。將來如研究DPO／ORPO，應只挑選清晰勝負pair，先核對base model license、人工重審、私隱及資料權利，再建立immutable dataset release同獨立holdout eval。

## 資源與限制

一個campaign預計約2–4MB；最多保留10個，hard budget約40MB內。系統不會自動刪除資料，到上限就拒絕建立新campaign。生成逐題手動觸發、node必須完全空閒、每個答案最多16KB及最多3次真正開始的attempt；processing lease容許server restart後安全續跑。

三票pair只提供方向性內部證據，唔係大型統計研究。測試亦只比較三個本地模式，無外部baseline，所以不能據此聲稱自家AI優於Gemini或其他外部模型。Latency只作營運參考，不直接計入勝負。

## Phase 3建議入口

1. 建立model registry及immutable release，將model digest、persona、prompt、runtime、license、訓練資料版本及Phase 2 summary hash綁在同一版本。
2. 將`both_bad`及低分題目交給人手分類：知識缺口先進RAG候選；語氣、格式、攻防或推理缺口先進SFT候選；安全問題先修prompt／policy並加regression case。
3. 所有候選資料先做人手匿名化、權利核對及accept/reject；保留固定30題作holdout，禁止直接拿去訓練造成eval leakage。
4. 數據量及一致性足夠後，先以小型SFT實驗開始；preference training另開研究gate，並以新campaign同舊release比較。
5. 只有eval證據、真GPU smoke、rollback方案及人工批准齊全，先考慮改production預設；系統本身仍然不自動切換。
