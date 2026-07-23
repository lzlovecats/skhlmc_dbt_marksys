# AI Workstation 操作手冊（新手版）

最後核對：2026-07-23

呢份文件係學校自家 AI 電腦嘅唯一安裝、驗收、日常操作同故障處理指引。你唔需要先明白
Linux、GPU、WSS 或 systemd。先喺下面揀你要做嘅事，再逐步跟；標示「進階」嘅章節，一般
日常操作可以跳過。

本文件涵蓋兩種電腦：

- **正式 Workstation**：Ubuntu AI 電腦，提供文字 AI，日後亦可提供 RAG、語音辨識同
  自家語音。
- **臨時文字機**：只喺正式 Workstation 維修、搬機或驗證期間使用，功能只有 Ollama
  文字生成。

網站係唯一控制中心。AI 電腦唔係網站副本，網站離線或安全檢查失敗時，Workstation
應該停止接新工作，而唔係自行繞過檢查。

## 先揀你要做嘅事

| 你而家要做咩 | 由邊度開始 |
|---|---|
| 第一次安裝正式 Workstation | [第 0 節](#0-遲啲-setup-實體-workstation由呢度開始) |
| 每日確認部機正常 | [第 1 節](#1-每日操作三分鐘檢查) |
| 網站顯示離線、故障或工作中斷 | [第 2 節](#2-故障處理先停再查) |
| 更新程式或切回上一版本 | [第 3 節](#3-更新同-rollback) |
| 啟用 ASR、RAG 或自家 TTS | [第 4 節](#4-進階啟用語音rag-或自家-tts) |
| 做正式上線驗收 | [第 5 節](#5-真機驗收同證據) |
| 暫時改用另一部 Ollama 電腦 | [第 6 節](#6-臨時文字機正式-workstation-維修時先用) |
| 建立、簽署或發布 release | [附錄 A](#附錄-a進階release-建立簽署同發布) |
| 開發後跑離線測試 | [附錄 B](#附錄-b開發及-release-驗證) |

## 開始前先認識 8 個詞

| 畫面或詞語 | 白話解釋 |
|---|---|
| Developer page／console | 網站內由 Developer 管理 Workstation 嘅頁面 |
| Workstation GUI | AI 電腦桌面嘅「AI Workstation Manager」程式 |
| Node | Workstation 同網站之間嘅安全連線程式 |
| Token／credential | 只顯示一次、用嚟證明呢部機身份嘅密碼；唔可以放入截圖、筆記、chat 或 git |
| Drain | 停止接新工作，已開始嘅工作會先完成 |
| Resume | 安全檢查通過後，重新接工作 |
| Preflight | 快速檢查基本設定、服務同模型 |
| Full health | 真實試用已啟用功能；比 Preflight 慢，但驗得更完整 |

## 任何人都要遵守嘅 7 條規則

1. **同一時間只可有一部 AI 電腦接單。** 唔可以用兩部機分流，亦唔可以保留自動
   standby。
2. **改設定、更新、切機或查嚴重故障前先 Drain。**
3. **Token 只輸入指定密碼欄。** 唔好貼入 terminal command、截圖、log、issue 或文件。
4. **唔好公開任何 AI 服務 port。** Ollama、Manager GUI 同 GPT-SoVITS 只可喺本機
   `127.0.0.1` 使用；RDP 同 SSH 只經 Tailscale。
5. **Health 失敗就保持 Drain。** 唔好為咗「暫時用住先」關閉檢查。
6. **唔好自行改 driver、kernel、CUDA、模型、production database、secret 或 R2 資料。**
   呢啲全部要逐項批准。
7. **唔好用 wildcard 或大範圍刪除 `/srv/lmc-ai`。** Uninstall 亦唔等於獲准刪資料。

如果你睇到嘅畫面要求輸入任意 shell command、package 名、URL、checkpoint path 或本機
路徑，先停止操作並通知 Developer。正式安全介面只會提供固定按鈕或固定 ID 選項。

## 0. 遲啲 setup 實體 Workstation：由呢度開始

呢一節畀第一次安裝嘅人。最少要有兩個角色：

- **現場人員**：可以接觸 Workstation、螢幕、鍵盤同 BIOS。
- **Developer**：有網站 Developer 權限，負責 credential、版本、模型同最後驗收。

建立 release、改 production、改 driver 或永久刪資料，需要另外批准；完成本節唔代表已獲
呢啲權限。

### 0.1 安裝日前要準備

逐項剔好先約安裝：

- [ ] Workstation：RTX 3060 8GB、最少 16GB RAM、500GB SSD。
- [ ] 如會正式訓練 GPT-SoVITS，RAM 建議先升至 32GB。
- [ ] 有線網絡、螢幕、鍵盤，同一位可以進入 BIOS/UEFI 嘅現場人員。
- [ ] Ubuntu Desktop 24.04.4 LTS 安裝手指。
- [ ] Developer 已批准 NVIDIA driver、kernel 同 CUDA 相容組合。唔好臨場追最新版。
- [ ] 已簽署嘅 `.deb`、signed envelope、固定 Ed25519 public key，同已核對嘅 public-key
  fingerprint。
- [ ] Private signing key 留喺離線簽署裝置，**唔可以**帶到 Workstation。
- [ ] Developer console 權限，同獲准使用嘅網站環境。
- [ ] Tailscale 管理權限、已設定 ACL，Mac 已安裝 Windows App。
- [ ] Ubuntu Desktop admin 帳戶。RDP 密碼要同日常 Linux 密碼不同。
- [ ] 安裝當日先建立一次性 node token，唔好預先寫入文件。
- [ ] 現階段要用嘅 signed Ollama model inventory 已準備。
- [ ] 已安排有人在場嘅 maintenance window，測 BIOS、冷開機、真正休眠、RTC 喚醒、
  斷電同非 Tailscale 網絡。

ASR model、GPT-SoVITS、RAG bundle 同 private R2 只喺準備啟用相應功能時先要準備。
第一次只上線文字 AI，唔需要同日完成所有未來功能。

### 0.2 安裝日執行次序

#### 步驟 1：記錄原始資料

記低以下內容，連同操作者同日期保存：

- 硬件 serial、GPU、SSD
- BIOS 設定
- Ubuntu image checksum
- driver、kernel、CUDA 版本
- 準備安裝嘅 release 版本

呢份記錄係日後查更新或硬件問題嘅基線。

#### 步驟 2：安裝 Ubuntu 同設定 BIOS

1. 安裝 Ubuntu Desktop 24.04.4 LTS。
2. 完成 security updates。
3. 建立獨立 Desktop admin 強密碼帳戶。
4. 按現有產品決定，正式 Workstation **唔啟用 full-disk encryption**，等冷開機後可以
   無人值守上線。電腦因此必須放喺鎖門房間。
5. 喺 BIOS/UEFI 開啟 RTC wake。
6. 實際測試一次休眠後由 RTC alarm 喚醒。

唔好自行升級 NVIDIA driver 或 kernel major version。冇 PiKVM，所以 BIOS、kernel hang、
GPU 黑畫面、路由器或網卡失效都要現場處理。

#### 步驟 3：驗證同安裝 `.deb`

以下命令要喺 repository 根目錄執行。先將命令入面嘅 `VERSION` 換成今次已批准版本。

```bash
sudo apt install python3-cryptography
python3 workstation/scripts/verify_release_artifact.py \
  --envelope workstation_release_stable.json \
  --public-key release-signing-public-key.pem \
  --component deb_package \
  --artifact lmc-ai-workstation_VERSION_amd64.deb
```

**成功標誌：** verifier 顯示 `ok:true`。見唔到呢個結果就停止，唔好安裝。

驗證成功後先執行：

```bash
sudo apt install ./lmc-ai-workstation_VERSION_amd64.deb
sudo /opt/lmc-ai-workstation/current/workstation/scripts/preflight_ubuntu.sh
```

`.deb` 會建立 `lmc-ai` service account、服務、資料目錄同 Desktop launcher。未配對之前，
`lmc-ai-node.service` 保持未啟動係正常。安裝亦唔會自動下載模型、寫 token、登入
Tailscale、改 driver、下載 GPT-SoVITS 或 deploy 網站。

#### 步驟 4：只經 Tailscale 開放遠端管理

按 Tailscale 官方 stable-track 指引安裝。唔好將 auth key 放入 shell history。完成 browser
登入後執行：

```bash
sudo tailscale up
sudo tailscale set --ssh
tailscale status
tailscale ip -4
```

喺 Ubuntu 開啟：

`Settings → System → Remote Desktop → Remote Login`

設定一個同 Linux 日常密碼不同嘅 RDP credential。Remote Login 係新登入 session，唔好
誤用 Desktop Sharing。

只容許 Tailscale interface 進入 RDP：

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow in on tailscale0 to any port 3389 proto tcp
sudo ufw enable
sudo ufw reload
```

由 Mac 做三項測試：

1. Windows App 連線到 `<MagicDNS 名稱或 Tailscale IP>:3389`。
2. Terminal 執行 `ssh <admin-user>@<MagicDNS-name>`。
3. 關閉 Tailscale或改用非 tailnet 網絡，再試 22 同 3389；兩者都必須連唔到。

最後喺 Workstation 執行：

```bash
ss -ltn | grep -E ':(11434|8765|9880|3389)\b'
```

Ollama、Manager GUI、GPT-SoVITS 只可見於 loopback。切勿公開 11434／8765／9880，
亦唔好加 router port forward 或 tunnel。

#### 步驟 5：配對網站

1. Developer 先確認舊 Workstation 已 Drain、停止服務，舊 credential 已 revoke。
2. 喺網站 Developer console 建立唯一 Workstation credential。
3. 立即複製只顯示一次嘅 token。
4. 喺 AI 電腦開啟「AI Workstation Manager」。
5. 去「配對網站」，填電腦名稱、網站 HTTPS URL 同一次性 token。
6. 按「保存並重新連線」。

**成功標誌：**

- GUI 顯示「配對資料已安全保存，網站亦已接受新 Node 連線」。
- 網站顯示新 Workstation online。
- 網站只得一個 enabled credential。

如果 token 或 WSS 未喺限時內獲網站接受，GUI 會還原舊設定。唔好不停重按或建立多個
credential，先去[第 2 節](#2-故障處理先停再查)。

#### 步驟 6：安裝文字模型

喺 Workstation GUI：

1. 按「先查看已簽署模型大小／雜湊」。
2. 核對畫面顯示嘅模型、精確大小、digest 同總下載量。
3. Developer 確認無誤後，按「批准並安裝上述模型」。
4. 等待下載完成同逐一 digest 驗證。
5. 按「執行 Preflight」。

模型名只由 `ai_model_config.py` 嘅 `lmc_ai_required_models()` 決定。本文件唔抄寫 model
tag，避免文件過期。Workstation 開機、health check 同普通網站 request 都唔會自行下載、
升級或刪除模型。

**成功標誌：** Preflight 通過，網站顯示正確 model profile、ready 狀態同預期 capability。

#### 步驟 7：設定每日休眠同喚醒

喺 Workstation GUI「每日休眠／喚醒」：

1. 剔選啟用排程。
2. 填香港時間嘅 Suspend 時間同 RTC wake 時間。
3. 按「保存排程」。
4. 有人在場時實測一次：
   - 冇工作時按時休眠
   - 有工作時延遲休眠
   - 工作完結後先休眠
   - RTC 正常喚醒
   - 網站重新顯示 WSS 連線

有 active job 時系統會每分鐘再檢查，唔會中斷工作。GUI 嘅「臨時保持喚醒」只跳過
scheduled suspend，唔會改 RTC 排程或停止 active job。

#### 步驟 8：收集證據，先驗收後 Resume

跟[第 5 節](#5-真機驗收同證據)收集自動報告，再完成人手測試。全部 gate 合格先
Resume 同開放正式工作。

### 0.3 Go-live gate

以下每項都要有記錄，唔接受「睇落有著」：

- [ ] `collect_ubuntu_evidence` 成功。
- [ ] 報告內刻意保留嘅 `manual_gates_complete:false` 已由人手測試補足。
- [ ] Mac RDP、Tailscale SSH 同非 tailnet 拒絕全部通過。
- [ ] 冷開機、休眠、RTC wake、斷電恢復同 WSS 重連全部通過。
- [ ] 文字模型所有模式、GPU、更新同 rollback 測試通過。
- [ ] 網站只顯示一部 Workstation online，capability 正確。
- [ ] 用測試帳戶做一次完整文字對話，結果正常。
- [ ] Logs、diagnostic、browser report 同 evidence 冇 token、signed URL、錄音、
  transcript 或會員資料。
- [ ] 舊機 credential 已 revoke，舊 service 已停止同 disable。
- [ ] 如今次同時啟用 Voice／RAG／ASR／TTS，已完成相應 full health、私隱、保留期、
  direct-R2 同 latency gate。

Voice 驗收要由 `workstation.scripts.verify_voice_latency` 接受至少
**20 個全部成功嘅 local warm turns**。詳細做法見[第 5 節](#5-真機驗收同證據)。

任何 gate 失敗：保持 Drain，唔好繞過 health。要退回舊機，必須重新建立唯一 credential
同重新驗收，唔可以同時開兩部機。

## 1. 每日操作：三分鐘檢查

日常操作優先用網站 Developer page。只喺網站控制唔到時，先用 Tailscale RDP／SSH。

### 每日開工

1. 開網站 Developer page。
2. 確認只得一部 Workstation。
3. 確認狀態：
   - Online
   - Ready
   - Drain = 否
   - Health 通過
   - 冇 active operation 或 fault
   - Capability 同當日要用嘅功能一致
4. 用不含個人資料嘅測試句做一次文字回應。
5. 如果所有項目正常，就唔需要再按其他按鈕。

### 每日收工

通常唔需要手動關機。確認冇卡住嘅 operation，同下一個 power action 時間正確即可。
如有短期活動唔想部機按時休眠，可設定有截止時間嘅「臨時保持喚醒」，用完立即取消。

### Terminal 後備命令

以下命令只喺 GUI／網站唔可用時，由 Developer 經 Tailscale SSH 執行：

```bash
sudo -u lmc-ai /usr/bin/python3 -m workstation.scripts.workstationctl status
sudo -u lmc-ai /usr/bin/python3 -m workstation.scripts.workstationctl drain
sudo -u lmc-ai /usr/bin/python3 -m workstation.scripts.workstationctl resume
sudo journalctl -u lmc-ai-manager.service -u lmc-ai-node.service -n 200 --no-pager
sudo /opt/lmc-ai-workstation/current/workstation/scripts/diagnostic.sh
```

`resume` 只可喺問題已處理、health 通過同網站狀態已核對後執行。

## 2. 故障處理：先停、再查

### 通用次序

1. **Drain。**
2. 記低時間、網站顯示狀態、operation ID 同做緊咩。
3. 唔好反覆按同一個更新、reboot、配對或工作按鈕。
4. 睇下面最接近嘅情況。
5. 問題處理後跑 Preflight；涉及已啟用 Voice／RAG／ASR／TTS 就跑 full health。
6. 網站再次顯示新 heartbeat、正確 health 同唯一 Workstation，先 Resume。

### 情況 A：網站顯示 Workstation 離線

依次檢查：

1. Workstation 有冇電，網線同路由器係咪正常。
2. Tailscale 係咪 online。
3. Workstation GUI「網站配對」係「已連線」定「未連線／等待心跳」。
4. 用 Terminal 後備命令查看 status 同最近 200 行 log。
5. 如果啱啱 reboot 或 restart Node，等新 heartbeat，唔好因 browser request 中斷就重按。

未證實 credential 失效之前，唔好建立另一個 token。需要重新配對時，先 revoke 舊
credential，確認舊 socket 已斷線，再建立唯一新 credential。

### 情況 B：Health／Preflight 失敗

1. 保持 Drain。
2. 只跑一次對應檢查，保存完整錯誤碼。
3. 執行 `diagnostic.sh` 產生診斷資料。
4. 核對磁碟空間、GPU、模型 inventory、WSS 同已啟用 capability。
5. 唔好用 restart loop、停用檢查或臨時下載另一個 model 掩蓋問題。

### 情況 C：狀態係 `faulted` 或 operation 係 `interrupted`

Manager restart 時，未收到 terminal ACK 嘅工作會標成 `interrupted`，呢個係保守設計。

1. 核對網站 job 狀態。
2. 如涉及檔案，核對 R2 intent 同清理狀態。
3. 核對 GPU 仲有冇舊 process。
4. 只有確定工作實際結果後，先喺 GUI 確認 reconciliation。
5. 唔好將舊工作當成功，亦唔好用同一 operation ID 直接重跑。

### 情況 D：語音已合成，但網站話失敗

本機合成完成唔等於整個工作成功。Node 仲要將檔案直傳 private R2，再收到網站 finish
ACK。上載失敗、取消或 Node restart 都會係 failure／interrupted，唔可以人手改成成功。

### 一定要現場處理

- 入唔到 BIOS
- kernel hang
- GPU 黑畫面
- 網卡或路由器失效
- 冷開機失敗
- RTC wake 失敗

## 3. 更新同 rollback

### 一般程式更新

喺 Workstation GUI「程式更新／Rollback」：

1. 確認冇 active operation。
2. 選擇已批准嘅更新頻道。一般正式機用 Stable。
3. 按「立即安全檢查更新」一次。
4. 系統會自動 Drain、驗簽、核對 hash／相容性、切換程式、重啟同做 full health。
5. 等網站出現新 heartbeat、正確版本同 health 結果。
6. 成功先 Resume。

任何 gate 失敗，系統只會切回 previous app release。Model、driver、dataset、RAG 同
GPT-SoVITS 唔會跟 app rollback 一齊刪或倒退。

### 手動切回上一版本

只喺 Developer 已確認需要時，按「切回上一版本」一次。Rollback 會使用唯一 previous
slot，完成後仍要 full health 同人手確認先 Resume。唔好用 rollback 處理 driver、model、
RAG、dataset 或資料問題。

### 幾時一定要重新安裝 `.deb`

自動 app update 只更新 `/opt/lmc-ai-workstation/releases/` 內程式。如果版本改到以下內容，
要安排 maintenance window，重新驗證並人手安裝 signed `.deb`：

- systemd unit
- Debian dependency
- apt policy
- Desktop launcher
- Ollama service override

### 資料唔會因 uninstall 自動刪除

Uninstall／purge 會保留 credentials、`/var/lib` 同 `/srv/lmc-ai`，避免誤刪資料。任何永久
刪除都要另一項明確授權。容量不足時，先 Drain，再按明確 dataset／checkpoint ID 移去同一
filesystem 嘅隔離目錄；觀察一個操作週期後，先考慮獲授權永久刪除。

## 4. 進階：啟用語音、RAG 或自家 TTS

正式上線次序係：

1. **文字 AI + server-side persona**
2. **RAG**
3. **ASR／Voice**
4. **自家 TTS**

每一階段獨立驗收。後一階段未完成，唔會阻止已驗收嘅文字 AI。Workstation 失敗時會
fail closed，唔會靜默將私人內容交去 Gemini／OpenRouter。

### 4.1 ASR：廣東話語音辨識

`.deb` **唔會自動安裝 ASR runtime 或下載 model**。只喺 Developer 已批准啟用 ASR 時，
經 Tailscale SSH／RDP 建立獨立 Python environment：

```bash
sudo apt install python3-venv
sudo install -d -o lmc-ai -g lmc-ai -m 0750 \
  /srv/lmc-ai/vendor/asr-runtime /srv/lmc-ai/models/asr
sudo -u lmc-ai python3 -m venv /srv/lmc-ai/vendor/asr-runtime
sudo -u lmc-ai /srv/lmc-ai/vendor/asr-runtime/bin/pip install \
  -r /opt/lmc-ai-workstation/current/workstation/requirements-asr.txt
```

之後要另外將已批准嘅完整官方 Qwen ASR model 放入
`/srv/lmc-ai/models/asr/<model-id>`。Runtime 只接受絕對本機目錄，唔會喺 request 時
補下載。

設定 `/etc/lmc-ai-workstation/config.json` 時，只使用已安裝 model 目錄同獨立 runtime：

```json
{
  "enabled": true,
  "model": "/srv/lmc-ai/models/asr/MODEL_ID",
  "device": "cuda",
  "compute_type": "float16",
  "runtime_python": "/srv/lmc-ai/vendor/asr-runtime/bin/python"
}
```

另要準備一段經人手核對、無會員資料嘅短廣東話 canary：

- `/srv/lmc-ai/health/asr-cantonese.wav`
- `/srv/lmc-ai/health/asr-cantonese.txt`

兩個檔由 root 擁有，group 為 `lmc-ai`，mode 0640。Full health 會真實轉錄同對答案；缺檔
或唔吻合就撤銷 ASR capability。完成設定後：

```bash
sudo systemctl restart lmc-ai-manager.service lmc-ai-node.service
sudo -u lmc-ai /usr/bin/python3 -m workstation.scripts.workstationctl full-health
```

### 4.2 RAG

RAG 只可使用網站已 review、可撤回、有 provenance 同已簽署嘅 bundle。

1. 網站 review／publish bundle。
2. 喺 Workstation GUI 先查看已簽署 artifact。
3. 按「下載、建立及啟用 RAG」。
4. Workstation 會驗 signature／hash，喺新目錄建立 index。
5. 成功先原子切換 `current`；失敗會保留上一版。
6. 跑 full health，確認 retrieval 同引用。

即時網頁搜尋仍然係另一個外部 Provider 功能，唔係本地 RAG。

### 4.3 自家 TTS

先依 [`AI_TRAINING_RUNBOOK.md`](AI_TRAINING_RUNBOOK.md) 準備單一已授權 speaker dataset。
訓練完成**唔會自動啟用**。人工 blind listening、廣東話讀音、latency、consent、
retention 同 deletion gate 全部通過後，Developer 先批准 voice。

同一把聲需要來自同一 training run 嘅 GPT `.ckpt` 同 SoVITS `.pth`。舊 checkpoint
family 或 runtime 不明時，保留原檔，唔好直接啟用。

以下係命令模板，必須先將每個大寫值換成今次已批准嘅 ID／檔名，唔好原樣執行：

```bash
sudo -u lmc-ai env PYTHONPATH=/opt/lmc-ai-workstation/current \
  /usr/bin/python3 -m workstation.scripts.approve_gpt_sovits_voice \
  --gpt-weight /srv/lmc-ai/checkpoints/DATASET_ID/APPROVED.ckpt \
  --sovits-weight /srv/lmc-ai/checkpoints/DATASET_ID/APPROVED.pth \
  --reference-audio /srv/lmc-ai/models/gpt-sovits/reference.wav \
  --reference-text /srv/lmc-ai/models/gpt-sovits/reference.txt \
  --output-root /srv/lmc-ai/models/gpt-sovits/voices/APPROVED_VERSION \
  --model-version APPROVED_VERSION
```

指令會 hash 所有 artifact 同建立 `active-receipt.json`，但唔會重啟 service。再將同一
model version 寫入 root-owned Workstation config，然後跑 full health 同[第 5 節](#5-真機驗收同證據)
嘅 Voice browser 驗收。

Voice fallback 次序係 local TTS → Azure TTS → text。呢個 fallback 唔等於容許將私人
內容自動由本地 LLM 轉交外部 LLM。

### 4.4 網站可以做同唔可以做嘅 remote control

Developer page 可以做：

- Drain、Resume、取消 operation、確認 restart reconciliation、full health
- 重啟 allowlisted Node／GUI／Ollama／GPT-SoVITS service
- Reboot、signed app update、previous-release rollback
- 設定每日 suspend／wake
- 以固定 ID 開關已安裝 LLM／ASR／RAG／TTS
- 查看／安裝 signed Ollama inventory，同安裝／rollback signed RAG bundle

Developer page唔可以做：

- 任意 shell command
- 任意 package、URL 或本機 path
- 首次安裝 ASR runtime／model
- 自動批准新訓練 checkpoint
- 改 driver、kernel 或 BIOS

Workload 設定變更會先 Drain。有 active job 時會顯示 busy，由 Developer等完成或明確
取消，唔會強搶 GPU。Health 失敗會還原上一份設定並保持 Drain。

## 5. 真機驗收同證據

每份報告都要記日期、操作者、release 版本、結果同保存位置。離線 tests 唔可以代替真機
測試。

### 5.1 收集 Ubuntu 自動證據

冷開機後先唔好登入 GNOME Desktop。由 Tailscale SSH 執行：

```bash
cd /opt/lmc-ai-workstation/current
sudo env PYTHONPATH=/opt/lmc-ai-workstation/current \
  /usr/bin/python3 -m workstation.scripts.collect_ubuntu_evidence \
  --output /var/lib/lmc-ai-workstation/acceptance/ubuntu-evidence.json
```

報告會檢查 Ubuntu、已安裝 package、boot service、timer、舊 Node、service account、
credential 權限、release tree、GPU、data filesystem、UFW、Tailscale、RTC 同基本文字
health。報告唔保存 token、prompt、錄音、逐字稿或 signed URL。

輸出只代表自動檢查。`manual_gates_complete` 會刻意保持 `false`，提醒你仲要做人手
RDP、真正 suspend／wake、斷電、故障注入同 browser 測試。

### 5.2 已啟用進階功能時跑 Full health

```bash
sudo -u lmc-ai /usr/bin/python3 -m workstation.scripts.workstationctl full-health
```

Full health 只測 config 標示為 enabled 嘅 capability。未啟用嘅 ASR、RAG 或 TTS
唔應該拖垮基本文字 readiness。

### 5.3 Voice browser latency

呢項只喺準備啟用 Voice 時做：

1. 用 Mac browser 打開練習頁，URL 加 `&acceptance=1`。
2. 開 DevTools Console，執行：

   ```javascript
   clearLmcAiPracticeAcceptanceReport()
   ```

3. 用本地 GPT-SoVITS 完成最少 20 個 warm 錄音回合，使用者正方同反方各完成一節。
4. Console 執行：

   ```javascript
   copy(JSON.stringify(lmcAiPracticeAcceptanceReport(), null, 2))
   ```

5. 將內容保存成 `voice-latency-browser.json`。報告只可有 turn、時間、provider 同成功
   狀態，唔可以有辯題、會員、錄音或逐字稿。
6. 用同一 release 驗證：

   ```bash
   /usr/bin/python3 -m workstation.scripts.verify_voice_latency \
     --input voice-latency-browser.json \
     --output voice-latency-verification.json
   ```

只有 verifier 接受 **20 個全部成功嘅 local warm turns** 先算通過。Fallback 或 failed
sample 唔可以靜默剔走。

### 5.4 人手驗收清單

基本文字 AI 上線要完成：

- [ ] 已驗簽 `.deb` clean install
- [ ] 冷開機、未登入 Desktop，Manager／Node 自動上線
- [ ] Mac RDP 同 Tailscale SSH 可用
- [ ] 非 tailnet 連唔到 22／3389
- [ ] Ollama／GUI 只見於 loopback
- [ ] 冇 active job 時按時休眠
- [ ] 有 active job 時延遲休眠，完成後先休眠
- [ ] RTC wake 同 WSS reconnect
- [ ] Power loss／Manager restart reconciliation
- [ ] 所有文字模式同多輪對話
- [ ] Update 成功、損壞 artifact 被拒絕、health 失敗自動 rollback
- [ ] Logs、diagnostic 同 evidence 私隱掃描

啟用相應功能前再加入：

- [ ] ASR 真實 canary、model load 同 interrupted failure
- [ ] RAG retrieval、引用、更新同 rollback
- [ ] GPT-SoVITS 真實合成、Voice fallback 同 browser latency
- [ ] Direct-R2 upload／download／delete
- [ ] Retention 同 consent withdrawal 演練
- [ ] ASR、LLM、TTS 順序取得唯一 GPU lease，冇 OOM

## 6. 臨時文字機：正式 Workstation 維修時先用

呢部機只提供文字生成，冇 Manager、RAG、ASR、GPT-SoVITS 或 direct-R2。切換期間
全系統仍然只可有一部機接單。

### 6.1 切換前

1. Drain 正式 Workstation，等所有工作完成。
2. 停止正式 Workstation 嘅 `lmc-ai-node.service`。
3. 喺 Developer console revoke 正式機 credential。
4. 確認舊 socket 已斷線。
5. 建立唯一新 credential，token 只輸入臨時機。

### 6.2 準備臨時機

支援有 systemd、Python 3 同已批准 NVIDIA driver 嘅 Ubuntu／Pop!_OS。Node 使用獨立、
無 sudo AI account。Ollama 仍用官方 installer 建立嘅 `ollama` service account。

```bash
nvidia-smi
systemctl status ollama --no-pager
ss -ltn | grep 11434
```

Ollama 必須只聽 `127.0.0.1:11434`。如唔係，先由 Developer 建立 systemd override，
設定 `OLLAMA_HOST=127.0.0.1:11434`，再 reload 同 restart。唔可以公開 11434。

### 6.3 安裝 CLI 同模型

```bash
sudo -iu AI_ACCOUNT
cd /path/to/skhlmc_dbt_marksys
python3 -m venv local_ai/.venv
local_ai/.venv/bin/pip install -r local_ai/requirements-node.txt
local_ai/.venv/bin/python -c \
  'from ai_model_config import lmc_ai_required_models; print(*lmc_ai_required_models(), sep="\n")'
```

將 `AI_ACCOUNT` 同 repo path 換成實際值。對輸出嘅每個 exact model tag 執行
`ollama pull MODEL_TAG`。Runtime 唔會自動下載、升級或刪除模型。

### 6.4 配對同 Preflight

```bash
local_ai/.venv/bin/python local_ai/lmc_ai_node.py configure
local_ai/.venv/bin/python local_ai/lmc_ai_node.py preflight
local_ai/.venv/bin/python local_ai/lmc_ai_node.py install-service
local_ai/.venv/bin/python local_ai/lmc_ai_node.py status
```

`configure` 會逐項問網站 HTTPS URL、電腦名稱同 token。Config 會保存喺 mode 600
credential file。Preflight 會測 required model、context、deadline 同 GPU offload；任何
model load、OOM、空白或逾時都會 fail closed。

成功後，喺 Developer console 確認唯一 WSS receipt、model profile、ready 同 Drain 狀態。
完成驗收先 Resume。

### 6.5 切回正式 Workstation

先停臨時機：

```bash
local_ai/.venv/bin/python local_ai/lmc_ai_node.py drain
sudo systemctl disable --now skhlmc-lmc-ai-node.service
```

再依次：

1. Revoke 臨時機 credential。
2. 確認臨時 socket 已斷線。
3. 建立一個新 token 畀正式 Workstation，唔可以重用臨時機 credential。
4. 正式機重新 configure、Preflight 同啟動 Node。
5. 確認網站只見一部可接單 Workstation。
6. 完成人手驗收後先 Resume。

## 附錄 A（進階）：Release 建立、簽署同發布

呢節只供 release builder／signer。建立檔案、簽署、上載、production migration 同 deploy
係分開授權，唔可以因為完成其中一步就自動做下一步。

### A.1 簽署 key

Private Ed25519 key 只留喺離線 signing device，唔可以放 repo、builder、Render、R2 或
Workstation。首次由離線裝置建立並備份：

```bash
openssl genpkey -algorithm ED25519 -out workstation-release-private.pem
openssl pkey -in workstation-release-private.pem -pubout \
  -out release-signing-public-key.pem
```

另行記錄同核對 public-key fingerprint。

### A.2 Ubuntu builder

Ubuntu amd64 builder 只取得 public key：

```bash
export WORKSTATION_RELEASE_PUBLIC_KEY_FILE=/secure/input/release-signing-public-key.pem
workstation/scripts/build_deb.sh dist
```

準備四個 immutable artifact：

- release archive
- `.deb`
- model inventory JSON
- RAG bundle

Model inventory 只可列出 `lmc_ai_required_models()` 允許嘅 exact name，並包括 registry
digest 同實際 bytes。RAG archive 根目錄要有 `documents.jsonl`。

### A.3 離線簽署

用 `create_release_manifest.py` 填清楚 website range、Ubuntu／driver／CUDA／Ollama、
GPT-SoVITS commit、DB migration requirement、R2 key 同本地 artifact。將 canonical
manifest 帶去離線 signing device：

```bash
workstation/scripts/sign_release_manifest.py \
  --manifest unsigned-workstation-manifest.json \
  --private-key workstation-release-private.pem \
  --output workstation_release_stable.json
```

簽署後，用 `verify_release_artifact.py` 分別重驗四個本地 artifact。上載 private R2、
提交 stable／candidate manifest、migration 同 deploy 都要另行授權。

### A.4 Production singleton gate

Rollout 前先對 production 做 read-only inventory，確認 `lmc_ai_nodes` 最多只有一行
`enabled=TRUE`。Singleton migration 見到多過一行會刻意 fail closed，唔會自行揀要保留
邊部機。

如果有多行：

1. 停止 rollout。
2. 喺仍運行舊版本嘅 Developer console 明確 revoke 唔再使用嘅 credential。
3. 再次 read-only 核對。
4. 另外取得 migration／deploy 授權。

唔好直接批量改 production rows。

## 附錄 B：開發及 release 驗證

```bash
./venv/bin/python -m pytest -q workstation/tests tests/test_lmc_ai.py
./venv/bin/python -m compileall -q workstation local_ai
node --check workstation/gui/static/app.js
node --check frontend/local_ai_practice/app.js
git diff --check
```

`.deb` 必須喺 Ubuntu amd64 builder 用 `workstation/scripts/build_deb.sh` 建立。macOS 冇
`dpkg-deb`，本機結果唔可以代替 clean Ubuntu install test。

Production migration、deploy、secret、artifact publish、driver change 同永久資料刪除，
仍然係分開授權動作。

## 附錄 C：系統設計同私隱界線

一般操作唔需要背以下內容，但故障或 code review 時要保持呢啲界線：

- `workstation/manager/`：模式、health、power、operation reconciliation。
- `workstation/node/`：由 Workstation 主動連網站嘅 authenticated outbound WSS。
- `workstation/workloads/`：Ollama、ASR、RAG、GPT-SoVITS、direct-R2。
- `workstation/privileged_helper/`：只接受固定 schema 嘅少量 root 動作。
- `workstation/gui/`：只聽 `127.0.0.1`。
- `workstation/packaging/`、`workstation/systemd/`：安裝 package、boot service 同 timer。
- `local_ai/lmc_ai_node.py`：臨時文字機 protocol client。

Manager 合法 mode 只有 `idle`、`text_serve`、`voice_coach`、`tts_training`、
`maintenance` 同 `faulted`。Voice 會等已開始嘅文字工作完成，同時拒絕新文字工作。
Training 唔會自動 pause，進行中禁止開始 Voice。

Workstation 只使用短期 signed R2 URL，唔保存長期 R2 secret。Operation timing 只保存
bounded 數值，唔保存 prompt、錄音、逐字稿或 signed URL。Transcript 同 final feedback
唔寫入 durable store，只可由 browser 下載。

網站端 raw audio、TTS output 同 direct-R2 probe 由既有 retention／sweeper contract
管理；實際限額以 `system_limits.py` 為唯一 code source。Delete 失敗會保留保守清理
記錄重試，唔會假裝已刪除。
