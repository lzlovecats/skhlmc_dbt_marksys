# 👾 自家 AI 電腦 Runbook（Pop!_OS）

呢部電腦只需要向網站建立 outbound WSS 連線；唔需要 Docker、公開 port、Cloudflare Tunnel 或新增 inbound firewall 規則。Ollama 必須只監聽 `127.0.0.1:11434`。

## 1. 準備 AI OS account

日常 account 保持日常使用；所有 node 指令同 systemd service 都用獨立 AI account 執行。Ollama 繼續用官方 installer 建立嘅 `ollama` system account/service，唔好改為 AI account。

切換到 AI account：

```bash
sudo -iu <AI_ACCOUNT>
```

安裝 Ollama 後確認 NVIDIA 同 localhost binding：

```bash
nvidia-smi
systemctl status ollama --no-pager
ss -ltn | grep 11434
```

如果 Ollama 唔係只綁 localhost，為 Ollama systemd override 設定 `OLLAMA_HOST=127.0.0.1:11434`，再 restart Ollama。

## 2. 安裝 node CLI

喺 repository 建立專用 venv：

```bash
cd /path/to/skhlmc_dbt_marksys
python3 -m venv local_ai/.venv
local_ai/.venv/bin/pip install -r local_ai/requirements-node.txt
```

Runtime 唔會自行下載模型。`fast`、`daily`、`deep` 三個模式嘅 model tag
只喺 `ai_model_config.py` 設定。先由同一設定列出目前需要嘅完整模型組合：

```bash
local_ai/.venv/bin/python -c 'from ai_model_config import lmc_ai_required_models; print(*lmc_ai_required_models(), sep="\n")'
```

對輸出嘅每一個 model tag 執行 `ollama pull <model-tag>`。如果 `daily` 同
`deep` 使用相同 tag，列表會自動去重，只需下載一次。

舊模型檔唔會由程式自動刪除；佢哋亦唔會被 preflight、節點連線或工作路由使用。

## 3. Developer 建立 token 同命名

1. 網站開「開發者設定 → AI 服務 → 自家 AI 電腦」。
2. 輸入每部電腦自己嘅名稱並建立。
3. 立即複製一次性 token；raw token 之後唔會再顯示。
4. 以 AI account 互動設定（token 輸入唔會留喺 shell history）：

```bash
local_ai/.venv/bin/python local_ai/lmc_ai_node.py configure
```

Config 位於 `~/.config/skhlmc-lmc-ai/node.json`、mode 600，並由 AI account 擁有。
`configure` 亦會問係咪啟用每日自動運作：23:55 停收新工作、00:00
休眠、08:00 由 RTC 喚醒及恢復接單。預設不啟用；每部 node 各自設定，
唔影響多機登記及 Developer 手動選擇 active node 嘅做法。

## 4. Preflight 同 systemd

```bash
local_ai/.venv/bin/python local_ai/lmc_ai_node.py preflight
local_ai/.venv/bin/python local_ai/lmc_ai_node.py install-service
local_ai/.venv/bin/python local_ai/lmc_ai_node.py status
```

Preflight 會驗證 `nvidia-smi`、Ollama、localhost binding，並以 8K context
逐一測試 Gemma 組合嘅三個實際模式。每次測試必須在 60 秒內完成、產生正式答案，
而且 GPU offload 至少 90%。只要有一個模式 load/OOM、空白、逾時或 offload
不合格，node 就唔可以 online/ready。
RTX 3060 最終結果以真機 preflight 為準；唔好移除 8K context 上限或直接設定
模型標示嘅更大 context。

Node online/ready 後，返 Developer console 手動按「選用呢部」。系統只容許選用
已完整通過 Gemma preflight 嘅電腦。`🔄 重新整理`只更新電腦狀態；
「取消選用所有電腦」會停止新工作、取消排隊工作，但容許目前生成完成。選中
電腦離線或 drain 時，服務會停低，唔會自動轉到另一部。

每位使用者在「自家 AI 專區」或「AI 辯論易」選擇回答模式：

- 快速回覆：Gemma E2B、`think=false`
- 日常預設：Gemma E4B、`think=false`
- 深入思考：Gemma E4B、`think=true`

每段 browser 對話固定一個模式，已有內容時切換會先確認並清除該段本機
對話。Gemma 4 經 Ollama 使用 boolean `think=true/false`，不提供
`low`／`medium`／`high` 強度；推理 stream 只在 node 內消耗，網站只轉送
最終答案。若推理完成但冇正式答案，node 會回報失敗並保留實際 token usage，
唔會再將空白答案記成成功。Vote Page 固定使用「快速回覆」；AI Coach 預設
使用「日常預設」，但仍可由使用者選擇快速回覆或深入思考。實際 model tag、
Thinking 開關同功能預設全部由 `ai_model_config.py` 統一設定。
「AI運作情況」會列出所有已登記電腦嘅在線、排隊、模型及 active
狀態；系統仍然只會將新工作送到 Developer 手動選中嘅一部，離線時唔會自動
轉另一部或轉雲端。

## 5. 可選自動休眠／RTC 喚醒

啟用排程前，先喺 BIOS/UEFI 確認 RTC wake 同 Linux suspend 正常，並確保
已安裝提供 `rtcwake` 嘅 `util-linux`。重新執行 `configure` 選 y，再執行一次
`install-service` 更新 timers：

```bash
local_ai/.venv/bin/python local_ai/lmc_ai_node.py configure
local_ai/.venv/bin/python local_ai/lmc_ai_node.py preflight
local_ai/.venv/bin/python local_ai/lmc_ai_node.py install-service
systemctl list-timers 'skhlmc-lmc-ai-auto-*'
```

23:55 drain 預留超過現行單次工作 180 秒上限，避免 00:00 休眠中斷正常工作。
00:00 root oneshot 只執行 `rtcwake --mode mem`；08:00 resume timer 由 AI
account 更新 config，唔會改變 active-node 選擇。首次啟用必須有人在場做一次
00:00–08:00 真機 smoke，因主機板、BIOS 同 RTC 實作會有差異。

## 6. 日常 account 要大量用 GPU

先用 AI account drain；佢會停止接新工作並等目前生成完成：

```bash
sudo -iu <AI_ACCOUNT> /path/to/repo/local_ai/.venv/bin/python /path/to/repo/local_ai/lmc_ai_node.py drain
```

日常工作完成後：

```bash
sudo -iu <AI_ACCOUNT> /path/to/repo/local_ai/.venv/bin/python /path/to/repo/local_ai/lmc_ai_node.py resume
```

## 7. Rotate、revoke、更新及故障檢查

- Rotate token：Developer console 操作後，舊 socket 即時斷線；再以 `configure` 輸入新 token，restart service。
- Revoke：即時取消該 node 嘅進行中工作並令 token 失效；metadata/usage 仍保留。
- 更新 code/dependencies：先 drain，更新 repo，同一 venv 重新安裝 pinned requirements，重新執行 `preflight` 同 `install-service`，最後 resume。模型 profile version 更新時，舊 preflight 會刻意失效；server handshake 亦會拒絕舊 profile 或未有任何完整模型組合嘅 node。
- 網站支援受控 Thinking 後，node hello 必須聲明 `thinking_control` capability；未更新的舊 node 會被 server 拒絕連線。部署網站版本前，先按上一項同步更新 AI 電腦程式並 restart service。
- 檢查：

```bash
local_ai/.venv/bin/python local_ai/lmc_ai_node.py status
sudo journalctl -u skhlmc-lmc-ai-node.service -n 100 --no-pager
ollama ps
nvidia-smi
```

官方參考：[Ollama GPU 支援](https://docs.ollama.com/gpu)、[Linux 服務](https://docs.ollama.com/linux)、[Gemma 4](https://ollama.com/library/gemma4)。
