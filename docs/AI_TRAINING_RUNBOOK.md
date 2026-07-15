# AI Training 錄音 → 本機廣東話 TTS 訓練手冊

最後核對：2026-07-14

本文件講解如何在一部 NVIDIA Linux Desktop，把本系統 AI Training 頁面下載的
`accepted` 錄音整理成可重現資料集，做廣東話 TTS 小資料微調、離線評估及保存模型。
最後一節簡述日後微調開源辯論 LLM 的路線。

> 這是研究環境 runbook，不是 production deployment 指引。第一輪目標是建立可重現
> baseline，不是由零 pretrain，也不是訓練完成便取代 Azure TTS。

## 0. 建議入口：本機拖放資料準備器

日常使用毋須逐段抄第 3–4 節命令。先在 workstation repo 根目錄啟動只綁定
`127.0.0.1` 的本機程式：

```bash
./tools/start_gpt_sovits_preparer.sh
```

Launcher 會優先使用 repo 的 `venv`，沒有便使用 `python3`；亦可直接執行
`python3 tools/gpt_sovits_preparer_app.py`。

Browser 會自動開啟「GPT-SoVITS 本機資料準備器」。把 AI Training 頁下載的單一錄音者
`recordings.json` 拖入頁面前，可以在「輸出根目錄」輸入絕對路徑或 `~/` 開頭的路徑並
按「套用路徑」；程式會重新核對該位置的可用空間。拖入檔案後，程式會自動：

1. 在所選輸出根目錄建立權限為 `700` 的獨立 workspace；
2. 驗證 manifest、單一 speaker、ID、稿句、重複 SHA-256 及下載 URL；
3. 直接從 R2 下載、retry、核對 size/hash/音訊 metadata，且不在 log 顯示 signed URL；
4. 只做 decode、32 kHz mono PCM16 轉檔，不做 denoise、音量 normalization 或變聲；
5. 按完整稿分組產生 `train`、`validation`、`test`，WebUI 指引只會引用 `train.list`；
6. 保存不含 signed URL 的 provenance、實際檔案 SHA-256、quality report 及 read-only raw；
7. 偵測 OS、CPU、RAM、磁碟、PyTorch／NVIDIA GPU、逐張 VRAM 及 driver，顯示保守的
   GPT-SoVITS batch、precision、epoch、save frequency 及 OOM fallback。

每個工作仍會在所選根目錄下建立權限為 `700` 的獨立 `tts-日期-編號` workspace。需要在
啟動時固定路徑亦可使用 `./tools/start_gpt_sovits_preparer.sh --output-root /absolute/path`。
若處理失敗，紅色提示會顯示已移除 signed URL 的實際驗證或下載錯誤；R2 401／403 亦會
保留安全錯誤碼，例如 `SignatureDoesNotMatch`，方便分辨 signer 設定同網絡代理問題。
自訂路徑如被 OS 拒絕，應改用目前登入帳戶可寫入的位置（通常係 home 目錄之下）。

參數 profile 鎖定正式 release `20250606v2pro`、commit
`d7c2210da8c013e81a94bfc7b811a477c99fd506`；epoch 沿用該 release default，batch 才按
本機硬件保守調整。開始訓練前仍須核對 checkout、code／weights／vocoder license；程式不會
把任何產物標成 production-ready。

拖放頁受 browser 安全限制，不能刪除原本 Downloads 內含短期 URL 的
`recordings.json`；完成後要自行把原檔移到 Trash。若 URL 已過期，重新 export 再拖入即可。
第 3–4 節保留作 audit、故障排查及人工重現參考。

## 1. Repo 現況

現時已經有：

- `/ai-training` 管理員頁面的「錄音審核 / Export」；
- `GET /api/ai-training/export/recordings.json?speaker=...`，只匯出 `accepted` 錄音；
- R2 直接下載連結、原檔 SHA-256、稿句、錄音格式、sample rate、時長及錄音者資料；
- 每段錄音的授權、審核及撤回流程。

現時未有：

- repo 內置的 TTS training script 或鎖定模型版本；
- 已 provision 的 dataset/model registry；相關 endpoint 回覆 `503` 是預期行為；
- 下載到 workstation 後，自動把撤回通知同步到本機 dataset/checkpoint 的機制。

因此本手冊會先在 workstation 建立不可變的本機 manifest。等
`ai_dataset_snapshots`、`ai_dataset_snapshot_items` 及 `ai_model_versions` 正式
provision 後，才以 server snapshot ID 取代這個過渡做法。整體 gate 亦以
[`ROADMAP.md`](ROADMAP.md#p4-自家粵語tts) 為準。

## 2. 標準 workstation

建議基準：

- Ubuntu 22.04/24.04 x86-64；
- 一張 NVIDIA RTX GPU；12 GB VRAM 可做小資料 baseline，16–24 GB 較寬鬆；
- 32 GB RAM、200 GB 可用 SSD；
- 全碟加密、獨立無 sudo 的 training account、加密 backup；
- training UI 只 bind `127.0.0.1`，不可開 public share link 或 router port forward。

先安裝由 Ubuntu package manager 提供、適合該 GPU 的 NVIDIA driver，再確認：

```bash
nvidia-smi
```

不要單憑 `nvidia-smi` 顯示的「CUDA Version」亂裝另一套 CUDA。PyTorch wheel 或
container 通常已帶所需 runtime；driver 只要兼容所選 build 即可。安裝前用
[PyTorch Start Locally](https://docs.pytorch.org/get-started/locally/) 及
[NVIDIA driver/CUDA matrix](https://docs.nvidia.com/datacenter/tesla/drivers/cuda-toolkit-driver-and-architecture-matrix.html)
核對。基本工具：

```bash
sudo apt update
sudo apt install -y ffmpeg jq git git-lfs libsox-dev
ffmpeg -version
ffprobe -version
```

如改用 container，依 NVIDIA 的
[Container Toolkit 安裝指引](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
設定 GPU runtime；不要把含錄音或 checkpoint 的目錄 mount 到其他共用服務。

## 3. 匯出及下載 accepted 錄音

### 3.1 匯出前 gate

只在以下條件全部成立時開始：

- 只選一位已授權的成年主聲線，不能把不同錄音者混成同一模型；
- 管理員已逐段聽過，狀態是 `accepted`；
- v0 最少約 30–60 分鐘乾淨語音；正式候選再增至約 1–3 小時；
- 重要人名、校名、辯論用語及常見粵語音節已有固定 test cases；
- 已安排收到撤回通知後，可停止訓練、停用模型及刪除本機副本的人。

少過 30 分鐘仍可做 smoke test 或 zero-shot comparison，但不要把結果當 production
candidate。微調通常先改善聲線相似度；讀音是否正確仍取決於文字前處理、粵語底模、
句庫覆蓋及評估，兩者不能混為一談。

### 3.2 取得 manifest

1. 登入 production `/ai-training`。
2. 到「錄音審核 / Export」，在「錄音者」輸入完整 user ID。
3. 按「按錄音者取得 accepted 錄音 R2 下載清單」。
4. 把檔案另存為 `recordings.json`，不要貼到 issue、chat 或 git。

manifest 最多 2,000 段；當前 presigned URL 最長只有效 3,600 秒。URL 本身等同短期
credential，不應寫入 log、commit 或長期保存。建立工作目錄：

```bash
export DATA_ROOT="$HOME/private-ai-training/tts-$(date +%Y%m%d)"
install -d -m 700 "$DATA_ROOT"/{raw,wav,provenance,metadata,eval,runs}
install -m 600 "$HOME/Downloads/recordings.json" "$DATA_ROOT/provenance/recordings.with-urls.json"
cd "$DATA_ROOT"
```

先檢查 speaker、數量、總時長及重複 hash：

```bash
jq -r '.items[].speaker_user_id' provenance/recordings.with-urls.json | sort -u
jq '{items:(.items|length), seconds:([.items[].duration_seconds]|add // 0)}' \
  provenance/recordings.with-urls.json
jq -r '.items[].audio_sha256' provenance/recordings.with-urls.json | sort | uniq -d
```

speaker 應該只有一個，重複 hash 應該沒有輸出。然後在一小時內下載：

```bash
jq -r '.items[] | [("audio/" + (.id|tostring) + ".source"), .download_url] | @tsv' \
  provenance/recordings.with-urls.json |
while IFS=$'\t' read -r rel url; do
  install -d -m 700 "raw/$(dirname "$rel")"
  curl --fail --location --retry 3 --output "raw/$rel" "$url"
done
```

逐檔核對 server 保存的 SHA-256：

```bash
jq -r '.items[] | [.audio_sha256, ("audio/" + (.id|tostring) + ".source")] | @tsv' \
  provenance/recordings.with-urls.json |
while IFS=$'\t' read -r sha rel; do
  printf '%s  %s\n' "$sha" "raw/$rel" | sha256sum --check -
done
```

任何一項不是 `OK` 都要停止；重新匯出 manifest 及下載，不可自行忽略或改 hash。
成功後移除會過期的 URL，保存 provenance：

```bash
jq 'del(.items[].download_url)' provenance/recordings.with-urls.json \
  > provenance/manifest.lock.json
sha256sum provenance/manifest.lock.json > provenance/manifest.lock.sha256
rm provenance/recordings.with-urls.json
chmod -R go-rwx "$DATA_ROOT"
```

`manifest.lock.json`、原始 audio hash 及之後記錄的 base model digest，合起來才足以重現
一次實驗。原始錄音保持 read-only，不要直接覆寫：

```bash
find raw -type f -exec chmod 400 {} +
```

## 4. 整理音訊、文字及 split

以下 32 kHz recipe 以 GPT-SoVITS V2/V2Pro baseline 為目標；如果當日審核後選用的 model
family 要求另一 sample rate，應依該版本 config 另建 derived WAV，並把決定寫入 run
config，不能混用 checkpoint。轉檔只做 decode、resample 及 channel conversion；不要預設
套 denoise、normalization、pitch shift 或 speed change，否則會改變聲線或製造 artifact。

```bash
jq -r '.items[].id' provenance/manifest.lock.json |
while IFS= read -r id; do
  out="wav/$id.wav"
  ffmpeg -nostdin -v error -y -i "raw/audio/$id.source" -map 0:a:0 \
    -ac 1 -ar 32000 -c:a pcm_s16le "$out"
done
```

抽查所有輸出都可讀，再列出技術異常：

```bash
find wav -type f -name '*.wav' -print0 |
while IFS= read -r -d '' file; do
  ffprobe -v error -select_streams a:0 \
    -show_entries stream=sample_rate,channels -of csv=p=0 "$file"
done | sort | uniq -c
```

預期只有 `32000,1`。另外要人工抽聽開頭、結尾、最短、最長及所有包含重要詞的錄音。
`prompt_text` 是 authoritative label；ASR 只可用來提示錯漏，不能自動覆寫稿句。

以下程式會沿用 server 未來 snapshot 的規則：以 `manuscript_id`（沒有便用
`script_id`）做穩定 hash，約 80/10/10 分成 train/validation/test，避免同一完整稿的
相鄰段落跨 split 洩漏。GPT-SoVITS 的 annotation 格式是
`wav_path|speaker_name|language|text`，廣東話 language code 是 `yue`。

```bash
export SPEAKER_NAME='replace-with-non-public-model-name'
python3 - <<'PY'
import hashlib
import json
import os
from pathlib import Path

root = Path(os.environ["DATA_ROOT"]).resolve()
speaker = os.environ["SPEAKER_NAME"].strip()
manifest = json.loads((root / "provenance/manifest.lock.json").read_text())
outputs = {name: [] for name in ("all", "train", "validation", "test")}

for item in manifest["items"]:
    text = " ".join(str(item["prompt_text"]).replace("|", "，").split())
    wav = root / "wav" / f"{item['id']}.wav"
    if not wav.is_file():
        raise SystemExit(f"missing WAV: {wav}")
    group = str(item.get("manuscript_id") or item["script_id"])
    bucket = int(hashlib.sha256(group.encode()).hexdigest()[:8], 16) % 10
    split = "test" if bucket == 0 else "validation" if bucket == 1 else "train"
    line = f"{wav}|{speaker}|yue|{text}"
    outputs["all"].append(line)
    outputs[split].append(line)

for name, lines in outputs.items():
    (root / f"metadata/{name}.list").write_text("\n".join(lines) + "\n")
    print(name, len(lines))
PY
```

如 validation 或 test 是空的，代表資料太少或分組太集中。不要把 test 搬回 train；先補
錄音，或在訓練前以整個 `manuscript_id` 為單位人工重分，再把決定及 seed 寫入
`provenance/split-notes.txt`。只把 `metadata/train.list` 餵給 training。

## 5. 首個可行 TTS baseline：GPT-SoVITS

選它做第一輪原因是 upstream 明列支援 `yue`、single-GPU、zero-shot 及少量資料微調，
而且 annotation format 與上一步相符。它是聲線／自然度 baseline，不代表已提供可硬控
Jyutping 的 phoneme-native contract。

官方 repo 更新頻密；不要永遠追 `main`。開始實驗當日重新核對
[upstream README](https://github.com/RVC-Boss/GPT-SoVITS)、release、code license、每一個
pretrained weight/vocoder 的 license 及 GPU 要求，然後鎖定一個 commit：

```bash
cd "$HOME"
git clone https://github.com/RVC-Boss/GPT-SoVITS.git
cd GPT-SoVITS
git checkout <audited-tag-or-commit>
git rev-parse HEAD | tee "$DATA_ROOT/provenance/gpt-sovits.commit"
```

upstream 現時的 Linux 安裝方式使用 Conda 及安裝 script；未有 Conda 可先依
[官方 Linux 安裝指引](https://docs.conda.io/projects/conda/en/latest/user-guide/install/linux.html)
安裝。`CU126` 或 `CU128` 必須按已安裝 driver、GPU 架構及當時 upstream support matrix
選擇，不要照抄：

```bash
conda create -n GPTSoVits python=3.10
conda activate GPTSoVits
bash install.sh --device <CU126-or-CU128> --source HF
python -c 'import torch; print(torch.__version__, torch.cuda.is_available(), torch.cuda.get_device_name(0))'
python webui.py
```

把 console 顯示的 local URL 開在同一部機；確認它只 listen localhost。WebUI 名稱會隨
版本變動，但流程應該是：

1. 先用未微調底模、固定 reference WAV 跑 zero-shot baseline，保存輸出。
2. Dataset/annotation 指向 `metadata/train.list`；audio 已切好，不再跑自動 ASR 或把
   validation/test 加入 training。
3. 語言選 `yue`，speaker/model 名用不包含真名的內部 ID。
4. GPT 與 SoVITS 必須選同一 upstream model family 的 pretrained weights；不要混用
   V2、V2Pro、V3/V4 checkpoint。
5. 12 GB VRAM 先由 batch size 1–2、fp16 及 upstream default epoch 開始；OOM 時先減
   batch/segment，再考慮 gradient checkpointing，不要改 dataset。
6. 每個 checkpoint 都用 validation 固定句試聽。train loss 繼續跌但未見句開始變差、
   漏字或複讀時，使用較早 checkpoint。
7. 用完全未入 training 的 `metadata/test.list` 做最後比較。

每次 run 最少保存：

```text
runs/<run-id>/
├── run.json                 # 日期、speaker ID、seed、hyperparameters、base model/commit
├── environment.txt         # nvidia-smi、Python、torch、pip freeze
├── manifest.lock.json      # 不含 signed URL
├── metadata/               # train/validation/test
├── checkpoints/            # private；不得上載 public model hub
├── samples/                # zero-shot、fine-tuned、Azure 對照
├── eval/                   # CER、讀音、人評、latency
└── SHA256SUMS
```

可用以下資料建立環境紀錄及最終 hash：

```bash
nvidia-smi > "$DATA_ROOT/runs/<run-id>/environment.txt"
python -VV >> "$DATA_ROOT/runs/<run-id>/environment.txt"
python -m pip freeze >> "$DATA_ROOT/runs/<run-id>/environment.txt"
find "$DATA_ROOT/runs/<run-id>" -type f ! -name SHA256SUMS -print0 |
  sort -z | xargs -0 sha256sum > "$DATA_ROOT/runs/<run-id>/SHA256SUMS"
```

## 6. 「聲似」不等於「讀音啱」

每次要用同一批 test sentences 比較：

- 現行 Azure voice；
- GPT-SoVITS zero-shot；
- 每個 fine-tuned checkpoint。

最低評估項目：

- **讀音正確率**：人名、校名、辯題詞、粵語多音字及 `tts_lexicon` 固定集逐項 pass/fail；
- **CER**：用同一 ASR pipeline 初篩，再由人核對，不能以 ASR 分數取代聽測；
- **MOS / 自然度**：blind A/B，評審看不到 provider/model 名；
- **speaker consistency**：同一 speaker reference、同一量度方法；
- **穩定性**：漏字、加字、複讀、爆音、長句失敗率；
- **first-audio latency**：同一 workstation、warm/cold 分開記錄。

Production 前仍先做 provider-neutral 文字正規化：`tts_lexicon` 長詞優先，把 `term` 改寫成
已審核 `reading`，再送入 Azure 或 custom TTS。GPT-SoVITS 的 `yue` frontend 並不保證可
直接用 Jyutping token 硬控每一個音。

如果文字覆寫仍不能通過固定讀音集，第二階段才研究 phoneme-native FastPitch/VITS：

- 建立「漢字 → 粵拼音節/聲調 token」的 versioned frontend；
- `tts_lexicon.jyutping` 覆寫必須有 automated regression tests；
- dataset label 同時保存原文、normalized text、Jyutping 及可接受變體；
- 再用較大量單一 speaker 資料做 transfer learning。

[NVIDIA NeMo TTS](https://docs.nvidia.com/nemo-framework/user-guide/latest/nemotoolkit/tts/configs.html)
有 FastPitch/HiFi-GAN fine-tune 工具，但沒有本 repo 可直接套用的廣東話 tokenizer recipe；
在完成粵語 frontend 前，它只是候選研究線，不能假裝 turnkey solution。

模型只有在固定集接近或優於 Azure、blind listening 合格、artifact 可重現及完成撤回演練
後，才可標成 candidate。接 production 時要另設 authenticated、TLS、rate-bounded GPU
service；Render 512 MB instance 不載 model，Azure 保留 fallback。

## 7. 撤回及事故處理

Production consent withdrawal 只會即時更新 server DB；已下載檔案及已訓練 checkpoint
不會自行消失。收到撤回或發現資料錯誤時：

1. 立即停止相關 training/inference，model 標成 `blocked`；
2. 以 speaker、recording ID 及 SHA-256 找出所有本機 manifest/run；
3. 刪除 workstation、backup 及 artifact storage 內的 raw/derived audio；
4. 所有曾使用該錄音的 checkpoint 停止部署並刪除；不能聲稱可從 checkpoint 精準
   「減去」一段錄音；
5. 由仍有效的 accepted 錄音重新建 snapshot 及重訓；
6. audit 記錄處理人、時間、受影響 run/model、刪除證據及新模型 ID，不保存已撤回內容。

任何 manifest hash mismatch、speaker 混合、授權版本不一致或不明 checkpoint，都按同一
流程 fail closed。

## 8. 未來：單 GPU 微調開源辯論 LLM

順序固定是 **eval → RAG → LoRA/QLoRA**，不是一下載文字便 full fine-tune：

1. 由 `/api/ai-training/export/llm.jsonl` 分批下載 `accepted` 內容；再次核對匿名化、使用權
   及撤回狀態。server dataset snapshot 尚未 provision 前，只作離線研究。
2. 先鎖定 20–50 條從未用作訓練的 eval cases，量度香港粵語自然度、引用準確、論證、
   追問、反駁、具體建議及私隱。
3. 先做 RAG。知識、規則及引用應留在可撤回、可更新的 approved corpus，不靠微調「背入」
   model。
4. 只有 RAG + prompt 已穩定但仍有一致風格/格式缺口，才由人把資料整理成
   `messages=[system,user,assistant]` instruction pairs。原始講稿或逐字稿不是天然 prompt/
   answer pair，不能直接塞入 SFT。
5. 一張 12 GB GPU 先測 3B–4B instruct model 的 4-bit QLoRA；batch size 1、短 context、
   gradient accumulation。實際 VRAM 受 context、optimizer 及 software version 影響，先用
   50–100 筆 smoke run 量度，不能假設一定 fit。

截至本文件核對日，[`Qwen/Qwen3-4B` model card](https://huggingface.co/Qwen/Qwen3-4B)
列出 `yue` 並使用 Apache-2.0，可作候選 baseline；開始實驗時仍要重新核對最新 model
card、license、chat template、context 及 maintainer 狀態。訓練 stack 可用 Hugging Face
[TRL SFTTrainer](https://huggingface.co/docs/trl/sft_trainer) +
[PEFT QLoRA](https://huggingface.co/docs/peft/main/package_reference/lora)，只訓練 assistant
response：

```bash
python -m venv "$HOME/venvs/debate-llm"
source "$HOME/venvs/debate-llm/bin/activate"
python -m pip install --upgrade pip
python -m pip install 'trl[peft]' bitsandbytes datasets
```

真正 training config 要鎖定：base model revision/digest、tokenizer/chat template、dataset
snapshot/hash、seed、max sequence length、quantization、LoRA rank/alpha/dropout、learning
rate、epoch、eval 結果及 adapter SHA-256。不要把私人 dataset、adapter 或 merged model
推到 public Hub。

只有 fine-tuned candidate 在同一 blind eval 顯著優於 RAG baseline，而且撤回可傳播、
latency/VRAM/維運成本可接受，才接入現有 provider abstraction 做 authenticated canary；
外部 provider 保留 fallback。

## 9. 一次實驗的完成定義

- [ ] 單一已授權 speaker，manifest 無 signed URL 並已鎖 SHA-256；
- [ ] 每個 raw audio hash、轉檔及 annotation 已核對；
- [ ] train/validation/test 按 manuscript 分組，test 從未入 training；
- [ ] base code、weights、license、environment、config、seed 及 artifact hash 已保存；
- [ ] zero-shot、fine-tuned、Azure 用同一固定集比較；
- [ ] 讀音、CER、blind listening、speaker consistency、latency、failure rate 已記錄；
- [ ] consent withdrawal 演練能找出並 block/delete 所有衍生物；
- [ ] 未通過 gate 的模型保持 research/blocked，不接 production。
