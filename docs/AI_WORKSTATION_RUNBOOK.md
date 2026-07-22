# AI Workstation Setup、安裝、驗收及操作 Runbook

本文件係自家 AI 電腦唯一 setup／日常操作／故障處理來源，涵蓋正式 Ubuntu AI
Workstation，同埋需要喺另一部電腦以 Ollama 接駁網站 WSS 嘅輕量文字 node。Repository
目錄名固定係 `workstation/`（單數），避免 package path、systemd unit、測試同文件出現
兩套命名。網站仍然係唯一 control plane；AI 電腦唔係離線網站副本。

目前產品只會由一部 Workstation 提供服務。另一部電腦嘅輕量 node 只用於搬機、維修或
驗證，唔可以同正式 Workstation 同時接單；切換前要先停止舊 service，再由 Developer
核對新 WSS connection。自家 AI 現階段只承諾本地公開權重 Gemma + server-side persona；
RAG 同自家 TTS 會按獨立 acceptance gate 逐步啟用。Ollama Web Search 已移除，網站
現有 Gemini／OpenRouter 搜尋屬另一套外部 Provider 功能。

## 系統範圍與目錄

- `workstation/manager/`：mode arbitration、durable reconciliation、health、power 及 IPC；
- `workstation/node/`：protocol v2 authenticated outbound WSS；
- `workstation/workloads/`：Ollama、官方 Qwen3-ASR、local RAG、GPT-SoVITS 同
  direct-R2 transfer；
- `workstation/privileged_helper/`：只接受固定 schema 嘅 suspend、idle reboot、
  allowlisted service restart、配對、排程及 verified release switch；
- `workstation/gui/`：只聽 `127.0.0.1` 嘅 Manager UI；
- `workstation/packaging/`、`workstation/systemd/`：`.deb` lifecycle 同 boot／timer units；
- `local_ai/lmc_ai_node.py`：另一部電腦只提供 Ollama 文字生成時使用嘅 protocol v1
  outbound WSS client；
- `workstation/tests/` 及 `tests/test_lmc_ai.py`：離線 contract tests。

安裝 `.deb` 唔會下載模型、寫入 token、登入 Tailscale、改 driver、pull GPT-SoVITS
upstream 或 deploy 網站；以上全部係分開審批及有證據嘅步驟。

## 發展次序與 Provider 分工

每一階段獨立驗收，未完成下一階段唔會阻止上一階段提供服務：

1. **現階段：本地 LLM + persona**：唯一 Workstation 跑公開權重 Gemma，server 擁有
   persona、prompt、fast／daily／deep routing、權限、限額同 accounting。Workstation
   離線或失敗時 fail closed，唔會靜默將私人內容轉交外部 Provider；
2. **下一階段：RAG**：只接收已 review、可撤回、有 provenance 嘅 signed corpus。
   Retrieval、引用、bundle activation、撤回傳播同 regression gate 全部通過後，先將
   `rag` capability 開為 true。外部 Provider 嘅即時網頁搜尋仍然係另一項明確選擇；
3. **再下一階段：自家 TTS**：只使用有 consent 嘅錄音，完成 dataset review、blind
   listening、廣東話發音、latency、retention 同 deletion gate，先啟用 local TTS。
   Voice fallback 次序係 local TTS → Azure TTS → text，唔會將 ASR／TTS readiness
   混入基本文字 LLM readiness。

本地 Workstation 適合 persona、日常文字推理、日後 private RAG 同自家 TTS；Gemini／
OpenRouter 保留畀使用者明確選擇嘅即時搜尋、暫時未本地化嘅錄音能力，以及需要外部模型
交叉核對嘅工作。兩邊共用網站嘅 auth、resource limit、operation accounting 同安全輸出
contract，但各自有獨立 provider attempt，唔設自動跨邊 fallback，亦唔恢復 multi-node。

4.12 rollout 前先喺 production 做 read-only inventory，確認 `lmc_ai_nodes` 最多只有一行
`enabled=TRUE`。Singleton migration 會喺多過一行時刻意 fail closed，唔會自行猜邊部機
應該保留；如有多行，先喺仍運行 4.11 嘅 Developer console 明確 revoke 唔再使用嘅
credential，再另行獲授權套用 migration／deploy。唔好直接批量改 production rows。

## 0. 遲啲 setup 實體 Workstation：由呢度開始

### 0.1 安裝日前要準備

- 實體機：RTX 3060 8GB、最少 16GB RAM、500GB SSD；GPT-SoVITS 正式訓練前建議
  升至 32GB RAM。準備有線網絡、螢幕／鍵盤，同可以進 BIOS/UEFI 嘅現場人員；
- OS／driver：Ubuntu Desktop 24.04.4 LTS installer，同已人手批准嘅 NVIDIA driver、
  kernel、CUDA 相容組合。唔好臨場自動追最新版；
- Release：由可信 Ubuntu amd64 builder 產生嘅 `.deb`、signed envelope、固定 Ed25519
  public key及已核對 fingerprint。Private signing key唔可以帶到 Workstation；
- 網站權限：Developer console 權限、獲准使用嘅測試／正式環境，以及安裝當日先建立嘅
  一次性 node token；唔好預先將 token 放入 repo、筆記或 shell history；
- 遙距管理：Tailscale tailnet admin／ACL 已準備，Mac 已安裝 Windows App，並有一個
  Ubuntu Desktop admin 帳戶。RDP credential 要同 Linux 日常 password 不同；
- AI artifacts：signed Ollama model inventory、Qwen3-ASR-1.7B 本地 model、固定
  GPT-SoVITS upstream及 license/provenance、已 review/publish 嘅
  signed RAG bundle；
- 雲端整合：網站端 node、private R2、WSS、release artifact endpoint 已在獲授權環境
  provision。Workstation 只收短期 signed URL，唔保存長期 R2 secret；
- 時段：預留有人在場嘅 maintenance window 做 BIOS、冷開機、RTC wake、斷電及非
  tailnet 測試。冇 PiKVM，以上項目唔可以純遙距完成。

### 0.2 安裝日執行次序

1. **記錄基線**：記下硬件 serial／GPU／SSD、BIOS 設定、Ubuntu image checksum、
   driver/kernel/CUDA 版本、操作者同日期；
2. **安裝 OS**：按第 2 節裝 clean Ubuntu、完成 security updates、設定獨立 Desktop
   admin、確認冇 full-disk encryption，再跑 preflight；
3. **驗簽及裝 package**：先用固定 public key 驗 `.deb`，成功先安裝；唔好以檔名、
   download page 或未簽 hash 代替；
4. **鎖網絡**：按第 3 節完成 Tailscale、Tailscale SSH、Remote Login、UFW。由 Mac
   試 RDP／SSH，再由非 tailnet 試 22／3389 必須失敗；
5. **配對網站**：按第 4 節用一次性 Workstation token 配對，等網站收到新鮮
   `hello.accepted` receipt；全系統只保留一個 enabled credential，未通過 acceptance 前
   唔好開放 production 工作；
6. **安裝及審批現階段 AI**：只裝 signed inventory 內嘅 Ollama model，完成 Gemma
   fast／daily／deep preflight。ASR、GPT-SoVITS 同 RAG 只喺啟用相應後續階段時，先做
   functional health、blind-listening approval 或 signed bundle atomic activation；
7. **設定電源**：按第 5 節設定每日 suspend／RTC wake；有人在場做一次 active-job
   delay、真正 suspend、RTC wake及 WSS reconnect；
8. **跑階段驗收**：基本文字服務按第 9 節產生 Ubuntu evidence，同完成 text latency、
   RDP、斷電、故障注入、update/rollback、retention 同 privacy smoke。Voice、RAG、ASR
   或 TTS report 只喺準備啟用相應 capability 時加入；
9. **先驗收、後啟用**：全部 gate 合格先開放唯一 Workstation 接單；保存 evidence、
   functional-health、簽署 manifest、版本及操作者紀錄。Production migration、deploy、secret、
   artifact publish 或 driver change仍然要逐項另行授權。

### 0.3 Go-live gate

以下每項都要有證據，唔接受以「服務好似有著」代替：

- `collect_ubuntu_evidence` 成功；佢刻意保留嘅 `manual_gates_complete:false` 已由下面
  人手紀錄補足；
- Mac RDP、Tailscale SSH、非 tailnet拒絕、cold boot、suspend／RTC wake、斷電恢復
  全部通過；
- Gemma 三個 mode、更新／rollback、GPU lease及所有基本文字 failure injection 通過；
- 準備啟用 Voice／RAG／ASR／TTS 時，先額外要求相應 direct-R2、functional health、
  retention、privacy gate，同由 `verify_voice_latency` 接受至少 20 個全部成功嘅 local
  warm turns；
- 網站顯示唯一 Workstation online及正確 capabilities，「同{LMC_AI_NAME}練習」（名稱由
  `ai_name.py` 唯一來源產生）以使用者正／反方各
  完成一節，正方先行、fallback及完場評語都正確；
- logs、diagnostic bundle、browser report同 evidence 冇 token、signed URL、錄音、
  transcript或其他會員資料；
- 唯一 enabled credential 只屬於已驗收 Workstation；舊機 credential 已 revoke，舊 service
  已停止／disable。

任何 gate 失敗，都先 drain Workstation、revoke 未通過驗收嘅 credential，唔可以繞過
health gate。要退回上一部機時，必須重新建立唯一 credential 同重新驗收，唔設 standby
node，亦唔容許兩部機同時接單。
App update失敗用第 7 節 signed previous-release rollback；模型、RAG、dataset或 driver
問題用各自上一個已批准 artifact／版本，唔可以靠 app rollback 或直接刪資料處理。

## 1. 安全界線

- v1 OS 係 Ubuntu 24.04.4 LTS，RTX 3060 8GB、16GB RAM、500GB SSD；正式
  GPT-SoVITS training 前建議先升至 32GB RAM。
- 按產品決定，v1 不啟用 full-disk encryption，目的是冷開機後可以無人值守
  上線。代價係實體失竊後 `/srv/lmc-ai` 的 consented dataset、checkpoint 同
  local model 可能被離線讀取。電腦必須放鎖門房間，Desktop admin 使用獨立強
  密碼帳戶，`lmc-ai` 係無 sudo、無 login shell service account。
- 網站只提供 allowlisted 管理動作；完整 Desktop／terminal 只經 Tailscale
  RDP／SSH。冇 PiKVM，所以 BIOS、kernel hang、GPU black screen、路由器或網卡
  失效必須現場處理。

## 2. Clean Ubuntu 及 `.deb`

1. 安裝 Ubuntu Desktop 24.04.4 LTS，完成全部 security updates；NVIDIA driver、
   kernel major version 必須由 Developer 人手批准，唔好由 Workstation updater 改。
2. 在 BIOS/UEFI 啟用 RTC wake，測試一次 `suspend` 後可以由 RTC alarm 喚醒。
3. 初次安裝亦唔可信任 `.deb` 檔名或網站顯示嘅 hash。先在另一部可信電腦取得
   pinned Ed25519 public key、兩欄 signed envelope 同 `.deb`，離線驗證
   `deb_package` component；verifier 成功輸出 `ok:true` 後先安裝：

   ```bash
   sudo apt install python3-cryptography
   python3 workstation/scripts/verify_release_artifact.py \
     --envelope workstation_release_stable.json \
     --public-key release-signing-public-key.pem \
     --component deb_package \
     --artifact lmc-ai-workstation_1.1.0_amd64.deb
   sudo apt install ./lmc-ai-workstation_1.1.0_amd64.deb
   sudo /opt/lmc-ai-workstation/current/workstation/scripts/preflight_ubuntu.sh
   ```

4. `.deb` 會建立 `lmc-ai` account、systemd units、資料目錄同 Desktop launcher；
   冇 node token 時 `lmc-ai-node.service` 有意保持未啟動。

## 3. Tailscale、RDP、SSH

Tailscale 套件來源會隨時間更新，所以按官方 stable-track 指引安裝，唔好將 auth
key 寫入 repo、shell history或 GUI。完成 browser authentication 後：

```bash
sudo tailscale up
sudo tailscale set --ssh
tailscale status
tailscale ip -4
```

Tailnet policy 只授權指定 Developer／管理裝置。Tailscale SSH 由 `tailscaled`
只在 tailnet IP 接管 port 22，唔需要公開 OpenSSH port。保留 key expiry；如果業務
上要關閉 expiry，必須記錄實體保管及失機 revoke 流程。

Ubuntu Settings → System → Remote Desktop → **Remote Login** 啟用 RDP，建立同
日常 Linux password 不同的 RDP credential。Remote Login 係新 login session；
唔好同 Desktop Sharing 混用。Mac 用 Windows App 連：

```text
<Workstation 的 Tailscale MagicDNS 名稱或 100.x.y.z>:3389
```

只容許 tailnet interface 入 RDP；切勿照一般 LAN 教學公開 3389：

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow in on tailscale0 to any port 3389 proto tcp
sudo ufw enable
sudo ufw reload
```

由 Mac 驗證 RDP 及 `ssh <admin-user>@<MagicDNS-name>`，再由非 tailnet 網絡確認
3389／22 連唔到。GUI、Ollama、GPT-SoVITS 必須只見於 loopback：

```bash
ss -ltn | grep -E ':(11434|8765|9880|3389)\b'
```

## 4. 配對、模型及 capability

1. 網站 Developer console 建立唯一 Workstation credential，立即複製一次性 token；
2. 在本機 GUI 填 Workstation 名、網站 URL、token。Token 只寫入 mode 600 credential
   file，GUI response／log／status 永不回傳。配對動作會以固定 service 名停止及
   disable 舊 `skhlmc-lmc-ai-node.service`，再 enable Workstation service；unit 亦宣告
   `Conflicts=`，避免同機兩個 client 同時 claim。GUI 只有收到 Workstation 嘅新鮮
   `hello.accepted` receipt 先會報配對成功；token／WSS 未於 bounded deadline 被網站
   接受，就會還原舊 config、token 同原有兩個 unit 狀態。Developer 仍必須核對唯一
   Workstation 已連線同通過 acceptance，先開放接單；
3. first-run 先按「查看已簽署模型大小／雜湊」。GUI 會下載細小 signed inventory，
   顯示每個 code-allowlisted model 的名稱、精確 bytes、digest 及合計大小。Developer
   再按「批准並安裝上述模型」及確認對話框，先會經 localhost Ollama `/api/pull`
   下載；完成後逐一重查 digest。安裝程式、開機、health、普通網站 request 都唔會
   pull model。Ollama 固定只聽 `127.0.0.1:11434`，模型放
   `/srv/lmc-ai/models/ollama`；
4. `.deb` **唔會自動安裝 ASR runtime 或下載 model**。要啟用時，Developer 經
   Tailscale SSH／RDP 明確建立獨立 Python environment，再安裝 repository 固定嘅官方
   `qwen-asr` package；唔裝入 Ubuntu system Python，亦唔容許普通 request／開機自動
   `pip install`：

   ```bash
   sudo apt install python3-venv
   sudo install -d -o lmc-ai -g lmc-ai -m 0750 \
     /srv/lmc-ai/vendor/asr-runtime /srv/lmc-ai/models/asr
   sudo -u lmc-ai python3 -m venv /srv/lmc-ai/vendor/asr-runtime
   sudo -u lmc-ai /srv/lmc-ai/vendor/asr-runtime/bin/pip install \
     -r /opt/lmc-ai-workstation/current/workstation/requirements-asr.txt
   ```

   另行將官方 `Qwen/Qwen3-ASR-1.7B` 完整下載／複製到例如
   `/srv/lmc-ai/models/asr/Qwen3-ASR-1.7B`；runtime 只接受絕對本地目錄，並以
   `HF_HUB_OFFLINE=1`、`TRANSFORMERS_OFFLINE=1` 運行，唔會 request-time 補下載。
   Workstation 唔再維護自製 accuracy benchmark、corpus 或 approval receipt；轉 model
   只需先安裝另一個完整本地 model 目錄，再改
   `/etc/lmc-ai-workstation/config.json` 內 `workloads.asr.model`。設定示例：

   ```json
   {
     "enabled": true,
     "model": "/srv/lmc-ai/models/asr/Qwen3-ASR-1.7B",
     "device": "cuda",
     "compute_type": "float16",
     "runtime_python": "/srv/lmc-ai/vendor/asr-runtime/bin/python"
   }
   ```

   Manager 固定使用 `language="Cantonese"`、batch 1。「同{LMC_AI_NAME}練習」開始錄音
   後會背景預載官方 Qwen worker；送出錄音時沿用同一個已載入 worker，完成即退出並
   釋放 CUDA context。預載被取消、voice session 結束或 240 秒冇收到錄音亦會強制
   卸載，之後 LLM／TTS 先可取得唯一 GPU lease。仍要準備一段經人工核對、無會員／
   個人資料嘅短廣東話
   canary，放成
   `/srv/lmc-ai/health/asr-cantonese.wav`，並將必須出現嘅正確文字放成同目錄
   `asr-cantonese.txt`。`/srv/lmc-ai/health` 由 root 擁有；用 Desktop admin 經 sudo
   寫入後設 root:`lmc-ai`、mode 0640，唔可以由 runtime request 改。Full health 會真實
   轉錄並比較 normalized expected text；缺檔或唔吻合即撤銷 ASR capability。改完設定後：

   ```bash
   sudo systemctl restart lmc-ai-manager.service lmc-ai-node.service
   sudo -u lmc-ai /usr/bin/python3 -m workstation.scripts.workstationctl full-health
   ```
5. GPT-SoVITS upstream 必須固定 commit/release、保存 license/provenance，依
   `docs/AI_TRAINING_RUNBOOK.md` 準備 dataset。GUI 訓練會按準備器已產生嘅
   `recommended_config.json`，順序做 text/BERT、HuBERT/32k、v2Pro speaker vector、
   semantic token、SoVITS、GPT；全部產物只寫入
   `/srv/lmc-ai/checkpoints/<dataset-id>`，同一 dataset 不會覆寫已有 run。訓練完成只寫
   `auto_activated:false`，唔會自動轉 production 聲線。人工 blind listening、讀音集、
   latency 同 consent gate 合格後，Developer 先明確執行：

   一次完整訓練產生嘅 GPT `.ckpt` 同 SoVITS `.pth` 係同一把聲嘅必要 pair；兩個檔要
   來自同一個 run，而且要同目前固定嘅 `v2Pro` runtime 相容。兩個 checkpoint 本身未夠
   啟用，仲需要固定 upstream/base models、reference WAV、對應 reference text、inference
   config 同 full-health 真實合成。舊 checkpoint family／runtime 未知時唔好直接當可用；
   先保留原檔，再用下面 activation command 同 full health 驗證。

   ```bash
   sudo -u lmc-ai env PYTHONPATH=/opt/lmc-ai-workstation/current \
     /usr/bin/python3 -m workstation.scripts.approve_gpt_sovits_voice \
     --gpt-weight /srv/lmc-ai/checkpoints/<dataset-id>/<approved>.ckpt \
     --sovits-weight /srv/lmc-ai/checkpoints/<dataset-id>/<approved>.pth \
     --reference-audio /srv/lmc-ai/models/gpt-sovits/reference.wav \
     --reference-text /srv/lmc-ai/models/gpt-sovits/reference.txt \
     --output-root /srv/lmc-ai/models/gpt-sovits/voices/<internal-approved-version> \
     --model-version <internal-approved-version>
   ```

   指令會 hash 所有選定 artifact、寫 fixed localhost inference config 同
   `active-receipt.json`，但唔會重啟 service；再將同一 `model_version` 寫入 root-owned
   Workstation config，跑 full health。平常 health 以 size/mtime fail closed，每六小時
   full health 重新計 SHA-256。唔可以由 request-time clone/pull／訓練／啟用；
6. RAG bundle 必須由網站 review/publish，Workstation 驗 signature/hash 後 build
   新 index，成功先原子切換 `current`；失敗保留上一版。

### 4.1 網站 remote full control

Developer page 會經同一條 outbound WSS 顯示 health、GPU/VRAM/溫度、目前 models、
Manager mode、active operation、power schedule、已安裝 ASR model ID、TTS voice ID 同
content-free audit。可執行嘅動作固定係：

首次由 Workstation 1.0.x 升到 1.1.0，舊 Node 本身未識 remote-control job，必須先經
舊 localhost GUI／Tailscale SSH 完成一次 signed update；網站會拒絕向 1.0.x 假裝發送
remote action。1.1.0 上線並回報新 version 後，先可由網站控制之後嘅 update／rollback。

- drain、resume、取消目前 operation、ack restart reconciliation、full health；
- restart allowlisted Node／GUI／Ollama／GPT-SoVITS service、reboot、signed app
  update／previous-release rollback、每日 suspend/wake schedule；
- 開關 LLM／ASR／RAG／TTS，並只可選擇 `/srv/lmc-ai/models/asr/<id>` 或已存在
  `active-receipt.json` 嘅 TTS voice ID；
- inspect／安裝 signed Ollama model inventory、inspect／安裝／rollback signed RAG bundle。

網站 request schema 唔接受 shell、package 名、URL、local path、checkpoint path 或任意
service。ASR runtime/model 同 TTS voice 仍然要先由 Developer 經 SSH／RDP 按本節安裝／
人工批准；remote page 只可啟用及切換已安裝 ID，唔會 request-time `pip install`、下載
公開 model 或自動啟用剛訓練 checkpoint。

Workload 設定變更會先 drain；有 active voice／training／text operation 時回覆 busy，
由 Developer 等完成或明確取消，唔會強制搶 GPU。空閒後 root helper 先保存上一份 typed
config、由 ID 解出固定 managed path、原子寫入，再跑 full health。任何 health gate 失敗
會即時 swap 回上一份 config、再跑舊設定 health，並保持 draining 供檢查；成功先按變更前
狀態 resume。每次 remote action 只在
`/var/lib/lmc-ai-workstation/remote-control-audit.json` 保存時間、action、outcome、error code，
最多 200 項；唔保存 prompt、錄音、transcript、token、URL 或 path。

網站 remote control 唔取代 Tailscale SSH／RDP：BIOS、driver/kernel、無網絡、system hang、
GPU black screen、磁碟／硬件故障同首次 model/runtime 安裝仍然要 remote desktop、SSH 或
現場處理。觸發 reboot／Node restart 後 WSS 會短暫斷線；以新 heartbeat、health 同 audit
確認結果，唔好因 browser request 中途斷開就重複觸發。

## 5. Manager mode 與電源

合法 mode 只有 `idle`、`text_serve`、`voice_coach`、`tts_training`、
`maintenance`、`faulted`。Voice reserve 會等已開始的 text job 完成，同時拒絕
新 text；training 不會自動 pause，進行中禁止開始 Voice。每個 managed workload
由 `systemd-inhibit` 明確阻止 sleep。

GUI 設每日 suspend／RTC wake。排程到時有 active job 就每分鐘再檢查，無限延遲；
job 完成後先 suspend。首次必須有人在場做 cold boot、suspend、RTC wake、Node
重連 rehearsal。

## 6. 日常操作及故障

```bash
sudo -u lmc-ai /usr/bin/python3 -m workstation.scripts.workstationctl status
sudo -u lmc-ai /usr/bin/python3 -m workstation.scripts.workstationctl drain
sudo -u lmc-ai /usr/bin/python3 -m workstation.scripts.workstationctl resume
sudo journalctl -u lmc-ai-manager.service -u lmc-ai-node.service -n 200 --no-pager
sudo /opt/lmc-ai-workstation/current/workstation/scripts/diagnostic.sh
```

Manager restart 時任何未有 terminal ACK 的 operation 會標成 `interrupted`，進入
`faulted`／drain；先核對網站 job 狀態、R2 intent 及 GPU process，才在 GUI 確認
reconciliation。唔可以直接當成功或重跑同一 operation ID。

日常操作優先用網站 Developer page；CLI 係 WSS／網站 control plane 唔可用時嘅
Tailscale SSH 後備。網站顯示嘅 profile selector只列 ID，任何出現任意 path／URL／
package input 嘅畫面都唔屬於本 runbook 定義嘅安全介面，應立即停止使用並 drain。

GUI 詳細狀態嘅 `recent_operations[].timings_ms` 只保存 bounded 數值，不保存 prompt、
錄音、逐字稿或 signed URL。真機 latency sign-off 要用呢啲 stage：`r2_download`、
`media_probe`、`asr`、`rag_retrieval`、`model_load`、`prompt_eval`、`generation`、
`tts_model_load`、`tts_synthesis`、`tts_probe`、`r2_upload`。

GPT-SoVITS 本地合成完成唔等於 operation 成功：Node 會先將 operation 轉到
`r2_upload`，直傳 private R2 並收到 server finish ACK，先寫 terminal success；上載
失敗、取消或 Node restart 都會寫 terminal failure／interrupted，唔會留下假成功。

## 7. 更新、rollback、資料清理

App update 必須 drain → idle → unprivileged staging → compatibility/signature/hash
verify → 原子切換 release symlink → restart/full health → resume；任何 gate 失敗只
切回 previous app release。Driver、model、dataset、RAG、GPT-SoVITS upstream 有
各自 canary／rollback，唔跟 app symlink 一齊刪。

Unprivileged staging tree 只作早期檢查；真正安裝時 root helper 會將 signed archive
邊複製到 root-owned 暫存邊重新驗 bytes／SHA，再由該 root-owned copy 安全解壓及逐檔
驗 `release-files.sha256`，避免 verify/copy 之間換檔。切換 symlink 後 helper 會先回覆，
再以受控非零 exit 由 systemd 從新 release 重載；Manager／Node／GUI 隨後重啟。
`/var/lib/lmc-ai-workstation/release/release-state.json` 連 parent directory 都由 root
擁有，只准 rollback 到 ledger 記錄嘅真正 previous release；
rollback 會消耗 previous slot，並須再次通過 full health 及 confirm 先 resume。

自動 release archive 只更新 `/opt/lmc-ai-workstation/releases/<version>` 內程式，唔會
改 `/lib/systemd/system`、Debian dependency、apt policy、Desktop launcher 或 Ollama
drop-in。涉及上述 package-level contract 嘅版本必須另外建立及離線驗證 signed `.deb`，
安排 maintenance window 人手安裝，再跑 clean-install／full-health gate；唔可以只靠 app
symlink updater 當已完成 package rollout。

GUI「切回上一版本」只會觸發獨立 rollback service；同自動更新共用單一 release
operation lock，先 drain 及等 idle，切換後重啟 Manager／Node／GUI，再通過完整 health
gate 先 resume。唔會由 GUI request 直接改 symlink，亦唔會中斷 active training／Voice。

五分鐘一次 shallow health 只做 bounded inventory／receipt 檢查；另一個 persistent timer
每六小時嘗試一次 functional full health（真 ASR sample、RAG retrieval、GPT-SoVITS
sample、R2 upload/download/delete、WSS、power、quota）。遇到 active managed job 會由
Manager fail closed，唔會搶 GPU 或中斷工作；下一輪 timer 再試。ASR／TTS functional
receipt 最多 24 小時，過期前未有成功 full health 就撤銷相應 capability。Direct-R2
成功 receipt 最多 7 小時，足以跨過六小時 timer；任何當前 R2 probe 失敗會立即刪除
舊 receipt 並撤銷 `direct_r2` capability，唔會沿用 stale success。

成功或失敗完成更新後，unprivileged staging 會自動清走；成功啟用 RAG 後亦會清走
下載／解壓 cache。大目錄硬 gate 係：dataset 120 GB、checkpoint 160 GB、cache
20 GB，以及任何 managed write 前保留最少 20 GB free space。到 quota 唔會靜默
覆蓋或自動刪 consented data；先 drain，再按 dataset/checkpoint ID 核對用途、consent、
active operation 同 rollback 需要，由 Developer 經 Tailscale SSH 將指定 ID 移去同一
filesystem 嘅隔離目錄，驗證一個操作週期後先獲授權永久刪除。禁止用 wildcard 或
recursive delete 指向 `/srv/lmc-ai` 根目錄。

GUI 可設定一個明確截止時間嘅「臨時保持喚醒」override（最長 7 日），亦可隨時取消；
override 只跳過 scheduled suspend，唔改每日 RTC 排程、唔停止 active job，過期後下一次
power timer 自動恢復原有規則。GUI 會同時顯示下一個 power action／check time。

網站端 retention 固定如下：成功 ASR 後立即刪 raw input；失敗 input 最多保留 15
分鐘供同一錄音重試；GPT-SoVITS output 最多 1 小時。背景 sweeper 每分鐘以 conditional
claim 清理，delete 失敗保持保守 intent 供下一輪重試。每個 Workstation node 同時只可
有一個 direct-R2 health probe；未 finish 嘅 probe 連 durable cleanup row 最多保留 15
分鐘，之後由同一 sweeper 重試 object-first delete。Transcript／final feedback
唔寫 durable store，只可由 browser 下載文字檔。Uninstall／purge 有意保留
`/etc/lmc-ai-workstation/credentials`、`/var/lib` 同 `/srv/lmc-ai`，避免 package remove
誤刪資料；任何真正資料刪除都係另一項明確授權。

## 8. Release build、離線簽署及 publish

private Ed25519 key 只留喺離線 signing device；唔可以放 repo、builder、Render、R2
或 Workstation。首次建立 key（離線執行）並另行備份／記錄 public-key fingerprint：

```bash
openssl genpkey -algorithm ED25519 -out workstation-release-private.pem
openssl pkey -in workstation-release-private.pem -pubout \
  -out release-signing-public-key.pem
```

Ubuntu amd64 builder 只取得 public key：

```bash
export WORKSTATION_RELEASE_PUBLIC_KEY_FILE=/secure/input/release-signing-public-key.pem
workstation/scripts/build_deb.sh dist
```

準備四個 immutable artifact：release archive、`.deb`、model inventory JSON、RAG
bundle。model inventory 每項只可係 `lmc_ai_required_models()` 列出嘅 exact name，並
包含 registry digest 同實際 bytes；RAG archive 根目錄要有 `documents.jsonl`。用
`create_release_manifest.py` 明確填 website range、Ubuntu／driver／CUDA／Ollama、
GPT-SoVITS commit、DB migration requirement、每個 R2 key 同本地 artifact，再將產生
嘅 canonical manifest 帶去離線 device：

```bash
workstation/scripts/sign_release_manifest.py \
  --manifest unsigned-workstation-manifest.json \
  --private-key workstation-release-private.pem \
  --output workstation_release_stable.json
```

簽署後用 `verify_release_artifact.py` 分別重驗四個本地 artifact。上載 private R2
及提交 `assets/workstation_release_stable.json`／`candidate.json` 係獨立 publish／deploy
動作，必須另有授權；server 只向 authenticated、未 revoke node 發短期 signed URL。
更新 timer 會做 drain → idle → R2 probe → signature/hash/compatibility → 原子切換 →
WSS、ASR、RAG、GPT-SoVITS、R2、power、quota full health。任何 gate 失敗會切回
previous release；model、RAG、driver、dataset 唔會跟 app rollback 一齊刪。

## 9. 真機 acceptance 記錄

以下全部要記日期、操作者、版本、結果及相關 health/benchmark report；離線 tests
唔可以代替：

冷開機後先唔好登入 GNOME Desktop，由 Tailscale SSH 執行自動證據 collector。佢會
fail closed 驗 Ubuntu point release、`.deb` version、五個 boot service、五個 timer、
舊 Node 已停用、service account／config／credential mode、immutable release tree、
RTX 3060／VRAM／data filesystem、UFW／Tailscale／RTC preflight，同基本文字服務 core
health；報告唔會保存 token、prompt、錄音、逐字稿或 signed URL：

```bash
cd /opt/lmc-ai-workstation/current
sudo env PYTHONPATH=/opt/lmc-ai-workstation/current \
  /usr/bin/python3 -m workstation.scripts.collect_ubuntu_evidence \
  --output /var/lib/lmc-ai-workstation/acceptance/ubuntu-evidence.json
```

輸出只代表 automated gates；`manual_gates_complete` 會刻意保持 `false`，唔可以用佢
取代下面 RDP、實際 suspend/wake、故障注入、retention 時鐘或 browser latency rehearsal。

準備啟用 Voice／RAG／ASR／TTS 時，另行執行並保存
`workstation.scripts.workstationctl full-health` 結果；呢個 future-capability gate 會要求真
ASR sample、RAG retrieval、GPT-SoVITS 同 direct-R2，但只會 probe config 明確標為
`enabled` 嘅 capability，唔會因未啟用嘅未來功能拖垮基本 Gemma + persona readiness。

啟用 Voice 時，Warm Voice latency 必須由 Mac browser 量度 end-to-end，而唔係用 Manager 內部 stage
時間代替。喺練習 URL 加 `&acceptance=1`，DevTools Console 先執行
`clearLmcAiPracticeAcceptanceReport()`，再以本地 GPT-SoVITS 完成最少 20 個 warm
錄音回合（使用者正／反方各一節）。完成後執行
`copy(JSON.stringify(lmcAiPracticeAcceptanceReport(), null, 2))`，將內容保存成 JSON；
報告只含 turn number、毫秒、local/Azure/text provider 同成功狀態，唔含辯題、會員、
錄音或逐字稿。用同一 release 驗證：

```bash
/usr/bin/python3 -m workstation.scripts.verify_voice_latency \
  --input voice-latency-browser.json \
  --output voice-latency-verification.json
```

Verifier 要求 20 個全部成功嘅 local warm turns，並硬性檢查 p50 首段文字 ≤8秒、
p50 首段聲音 ≤15秒、p95 首段聲音 ≤25秒；fallback／failed sample 唔會被靜默排除。

以下清單逐階段累加：基本 Gemma + persona go-live 先做 OS、網絡、電源、WSS、Gemma、
update／rollback 同文字對話項目；含 ASR／RAG／GPT-SoVITS／Voice／R2 嘅項目，只喺
啟用相關 capability 前先成為硬 gate。

- clean Ubuntu 24.04.4 用一個已驗簽 `.deb` 安裝；
- 冷開機、無 Desktop login，Manager／Node 自動上線；
- Mac Windows App 建立新 RDP session，Tailscale SSH 後備可用；非 tailnet 連唔到
  22／3389，Ollama／GUI／GPT-SoVITS 只見 loopback；
- idle scheduled suspend、active job 無限 delay、job 後 suspend、RTC wake、WSS reconnect；
- power loss／Manager restart reconciliation、duplicate operation／stale turn fail closed；
- RTX 3060 8GB warm p50 首段文字 ≤8秒、首段聲音 ≤15秒、p95聲音 ≤25秒，以及
  ASR/Gemma/GPT-SoVITS sequential GPU lease 無 OOM；
- ASR／model load／GPT-SoVITS／Azure／R2 interrupted/hash/MIME/duration/delete failure；
- update success、corrupt artifact rejection、full-health failure auto rollback；
- 多輪正方先行（使用者選正／反方各一次）、training blocks Voice、已開始 text 完成
  先交 Voice、local TTS → Azure → text fallback；
- 真 R2 15分鐘／1小時 delete smoke，同 logs／secrets／privacy scan。

冇 PiKVM；BIOS、kernel hang、GPU black screen、網卡或路由器故障仍需現場介入。

## 10. 喺另一部電腦安裝 Ollama 並接駁網站 WSS

呢個流程只建立輕量文字 Workstation client，唔提供 Manager、RAG、ASR、GPT-SoVITS 或 direct-R2。
佢只適合正式 Workstation 維修、搬機或驗證時使用，而且全系統任何時間只可由一部
AI 電腦接單。切換前先 drain／停止正式 Workstation，唔可以用兩部機做 load balancing。

### 10.1 準備獨立 account 同 Ollama

支援有 systemd、Python 3 同 NVIDIA driver 嘅 Ubuntu／Pop!_OS。日常 account 保持日常
使用；node CLI 用獨立無 sudo AI account，Ollama 繼續用官方 installer 建立嘅 `ollama`
service account。安裝完成後確認 GPU 同 localhost binding：

```bash
nvidia-smi
systemctl status ollama --no-pager
ss -ltn | grep 11434
```

Ollama 必須只聽 `127.0.0.1:11434`。如唔係，建立 systemd override 設
`OLLAMA_HOST=127.0.0.1:11434`，然後 daemon-reload 及 restart；唔可以公開 11434、加
router port forward、Cloudflare Tunnel 或 inbound firewall exception。網站連線係由
node 主動建立 authenticated outbound WSS。

### 10.2 安裝 Workstation CLI 同模型

```bash
sudo -iu <AI_ACCOUNT>
cd /path/to/skhlmc_dbt_marksys
python3 -m venv local_ai/.venv
local_ai/.venv/bin/pip install -r local_ai/requirements-node.txt
local_ai/.venv/bin/python -c \
  'from ai_model_config import lmc_ai_required_models; print(*lmc_ai_required_models(), sep="\n")'
```

對列出嘅每一個 exact model tag 執行 `ollama pull <model-tag>`。Runtime 唔會自動下載、
升級或刪除模型；fast／daily／deep 嘅 tag、8K context 同 thinking 設定只由
`ai_model_config.py` 決定。

### 10.3 建立 token、設定 WSS 及 preflight

1. 先 drain／停止正式 Workstation `lmc-ai-node.service`，確認所有 in-flight 工作完成；
2. 喺網站 Developer console revoke 現有唯一 credential。確認舊 socket 已斷線後，建立
   新 Workstation credential，立即複製只顯示一次嘅 token；
3. 以 AI account 執行互動設定，輸入網站 HTTPS base URL、電腦名稱同 token：

```bash
local_ai/.venv/bin/python local_ai/lmc_ai_node.py configure
local_ai/.venv/bin/python local_ai/lmc_ai_node.py preflight
local_ai/.venv/bin/python local_ai/lmc_ai_node.py install-service
local_ai/.venv/bin/python local_ai/lmc_ai_node.py status
```

Config 位於 `~/.config/skhlmc-lmc-ai/node.json`、mode 600。Client 會將 HTTPS base URL
轉成 `wss://<host>/api/lmc-ai/nodes/connect`，用 token 做 handshake；唔需要 public port。
Preflight 會逐一測試 required Gemma mode、8K context、60 秒 deadline 同最少 90% GPU
offload。任何 model load／OOM／空白／逾時／offload failure 都會 fail closed。

成功後喺 Developer console 核對唯一 WSS receipt、model profile、ready 同 draining 狀態。
正式 Workstation 恢復前，先執行：

```bash
local_ai/.venv/bin/python local_ai/lmc_ai_node.py drain
sudo systemctl disable --now skhlmc-lmc-ai-node.service
```

然後喺 Developer console revoke 呢部替代機嘅 credential，建立一個新 token 畀正式
Workstation，重新 configure／preflight／啟動正式 `lmc-ai-node.service`，確認網站只見
一部可接單 Workstation。Credential 唔可以喺兩部機之間共用。

### 10.4 Rotate、更新及故障檢查

- Rotate token 後舊 socket 會即時斷線；重新執行 `configure`，再 restart service；
- 更新 Workstation client code/dependencies 前先 drain，更新後重跑 pinned requirements、preflight 同
  install-service；
- model profile version 改變時，未同步嘅 client 會被 server 拒絕，必須先同步模型同程式；
- browser 對話只保存喺 browser，server 唔會 durable 保存 prompt 或 transcript；
- 本地 Workstation 離線時 fail closed，唔會靜默轉去 Gemini／OpenRouter。

```bash
local_ai/.venv/bin/python local_ai/lmc_ai_node.py status
sudo journalctl -u skhlmc-lmc-ai-node.service -n 100 --no-pager
ollama ps
nvidia-smi
```

## 11. 開發及 release 驗證

```bash
./venv/bin/python -m pytest -q workstation/tests tests/test_lmc_ai.py
./venv/bin/python -m compileall -q workstation local_ai
node --check workstation/gui/static/app.js
node --check frontend/local_ai_practice/app.js
git diff --check
```

`.deb` 必須喺 Ubuntu amd64 builder 用 `workstation/scripts/build_deb.sh` 建立；macOS 冇
`dpkg-deb`，所以唔可以用本機結果代替 clean Ubuntu install test。Production migration、
deploy、secret、artifact publish、driver change同永久資料刪除仍然係分開授權動作。
