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

下載兩個已核准 models；runtime 唔會自行下載：

```bash
ollama pull qwen3.5:9b
ollama pull qwen3.5:4b
```

## 3. Developer 建立 token 同命名

1. 網站開「開發者設定 → AI 服務 → 自家 AI 電腦」。
2. 輸入每部電腦自己嘅名稱並建立。
3. 立即複製一次性 token；raw token 之後唔會再顯示。
4. 以 AI account 互動設定（token 輸入唔會留喺 shell history）：

```bash
local_ai/.venv/bin/python local_ai/lmc_ai_node.py configure
```

Config 位於 `~/.config/skhlmc-lmc-ai/node.json`、mode 600，並由 AI account 擁有。

## 4. Preflight 同 systemd

```bash
local_ai/.venv/bin/python local_ai/lmc_ai_node.py preflight
local_ai/.venv/bin/python local_ai/lmc_ai_node.py install-service
local_ai/.venv/bin/python local_ai/lmc_ai_node.py status
```

Preflight 會驗證 `nvidia-smi`、Ollama、localhost binding 同兩個 models；先以 `think=false`、4K context 測試 9B。60 秒內失敗、load/OOM 或 CPU offload 超過 10% 就測 4B；兩者都失敗則唔會宣告 ready。RTX 3060 最終結果以真機 preflight 為準。

Node online/ready 後，返 Developer console 手動按「選用呢部」。`🔄 重新整理`只更新電腦狀態；「取消選用所有電腦」會停止新工作、取消排隊工作，但容許目前生成完成。選中電腦離線或 drain 時，服務會停低，唔會自動轉到另一部。

每位使用者在「自家 AI 專區」的回答模式 dropdown 選擇「快速回答」或「深入思考」，預設為快速回答；Developer console 不再提供全系統 Thinking 開關。每段 browser 對話固定一個模式，已有內容時切換會先確認並清除該段本機對話。Qwen 3.5 經 Ollama 使用 boolean `think=true/false`，不提供 `low`／`medium`／`high` 強度；推理 stream 只在 node 內消耗，網站只轉送最終答案。

## 5. 日常 account 要大量用 GPU

先用 AI account drain；佢會停止接新工作並等目前生成完成：

```bash
sudo -iu <AI_ACCOUNT> /path/to/repo/local_ai/.venv/bin/python /path/to/repo/local_ai/lmc_ai_node.py drain
```

日常工作完成後：

```bash
sudo -iu <AI_ACCOUNT> /path/to/repo/local_ai/.venv/bin/python /path/to/repo/local_ai/lmc_ai_node.py resume
```

## 6. Rotate、revoke、更新及故障檢查

- Rotate token：Developer console 操作後，舊 socket 即時斷線；再以 `configure` 輸入新 token，restart service。
- Revoke：即時取消該 node 嘅進行中工作並令 token 失效；metadata/usage 仍保留。
- 更新 code/dependencies：先 drain，更新 repo，同一 venv 重新安裝 pinned requirements，再 `sudo systemctl restart skhlmc-lmc-ai-node.service`，最後 resume。
- 網站支援受控 Thinking 後，node hello 必須聲明 `thinking_control` capability；未更新的舊 node 會被 server 拒絕連線。部署網站版本前，先按上一項同步更新 AI 電腦程式並 restart service。
- 檢查：

```bash
local_ai/.venv/bin/python local_ai/lmc_ai_node.py status
sudo journalctl -u skhlmc-lmc-ai-node.service -n 100 --no-pager
ollama ps
nvidia-smi
```

官方參考：[Ollama GPU 支援](https://docs.ollama.com/gpu)、[Linux 服務](https://docs.ollama.com/linux)、[Qwen 9B](https://ollama.com/library/qwen3.5:9b)、[Qwen 4B](https://ollama.com/library/qwen3.5:4b)。
