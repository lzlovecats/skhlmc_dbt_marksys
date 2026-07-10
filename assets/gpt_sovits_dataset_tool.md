# GPT-SoVITS Dataset Preparation Tool

下載 `ai_training.py` 匯出的 `tts_voice_dataset*.zip` 後，可用以下工具自動準備 GPT-SoVITS 訓練用資料。

## 用法

```bash
cd /Users/lzlovecats/Documents/GitHub/skhlmc_dbt_marksys-develop
python3 tools/prepare_gpt_sovits_dataset.py /path/to/tts_voice_dataset_wongsunchung.zip
```

工具會：

- 解壓 zip 到同名資料夾
- 讀取 `metadata.csv`
- 使用 `prompt_text` 產生 GPT-SoVITS `.list`
- 檢查 audio 檔是否存在
- 統計總錄音分鐘
- 偵測目前電腦的 CPU / RAM / NVIDIA GPU VRAM
- 根據硬件及錄音分鐘輸出保守 fine-tuning parameters
- 產生 `<experiment>_webui_fields.txt`，列出 WebUI 要填的欄位

GPT-SoVITS list 格式：

```text
/absolute/path/to/audio.wav|speaker|yue|文字
```

## 常用參數

指定輸出資料夾：

```bash
python3 tools/prepare_gpt_sovits_dataset.py ~/Downloads/tts_voice_dataset_wongsunchung.zip \
  --output-dir ~/Documents/tts_voice_dataset_wongsunchung
```

指定 speaker：

```bash
python3 tools/prepare_gpt_sovits_dataset.py ~/Downloads/tts_voice_dataset.zip \
  --speaker wongsunchung
```

指定 experiment/model name：

```bash
python3 tools/prepare_gpt_sovits_dataset.py ~/Downloads/tts_voice_dataset_wongsunchung.zip \
  --experiment wongsunchung_yue_v1
```

## GPT-SoVITS WebUI

### 開 WebUI

```bash
cd ~/Documents/AI/GPT-SoVITS
conda activate GPTSoVits
python webui.py
```

瀏覽器開：

```text
http://127.0.0.1:9874
```

如果要防止 MacBook sleep，另外開一個 Terminal：

```bash
caffeinate -dimsu
```

### 0 / Fine-Tuned Model Information

工具完成後，照 terminal 或 `<experiment>_webui_fields.txt` 輸出的值填：

```text
Experiment/model name: <experiment>
GPU Information: 0 CPU Training on CPU (slower)
Version of the trained model: v2Pro
```

### 1A / Dataset Formatting Tool

```text
Text labelling file: <output>/<experiment>.list
Audio dataset folder: leave blank
```

因為 `.list` 已經使用 audio 的絕對路徑，`Audio dataset folder` 留空即可。

然後按順序撳：

```text
1. Open Tokenization & BERT Feature Extraction
2. Open Speech SSL Feature Extraction
3. Open Semantics Token Extraction
4. Open Training Set One-Click Formatting
```

每一步等 output / terminal 完成，無 error 先下一步。

### 1B / Fine-Tuning

使用工具輸出的 `Recommended 1B fine-tuning parameters for this machine`。

Apple Silicon Mac / 無 NVIDIA GPU 的保守建議通常是：

```text
SoVITS batch size: 1
SoVITS total epochs: 4（快速 smoke test）或 8（較完整第一輪）
SoVITS text model learning rate weighting: 0.4
SoVITS save frequency: 2
SoVITS GPU number: 0
SoVITS checkboxes: 兩個都保持勾選

GPT batch size: 1
GPT total epochs: 10
GPT save frequency: 5
GPT GPU number: 0
GPT DPO training: 不勾選
GPT checkboxes: 兩個都保持勾選
```

次序：

```text
1. Open SoVITS Training
2. 等 SoVITS 完成
3. Open GPT Training
4. 等 GPT 完成
5. 去 1C-Inference 試聲
```

### 1C / Inference

```text
Reference audio: 選 audio/ 入面一段乾淨、無爆咪、無讀錯的 wav
Reference text: 填該段錄音的原文
Reference language: yue
Target text: 要測試生成的句子
Target language: yue
```

例如：

```text
Target text: 我哋今日嘅立場係正方，對方辯友忽略咗一個好重要嘅前提。
```

## 自動 parameter 建議邏輯

工具只做保守估算，不會保證最快。

- 有 NVIDIA GPU：按 `nvidia-smi` 偵測到的 VRAM 粗略放大 batch size。
- Apple Silicon Mac：預設用 CPU 參數，因為 GPT-SoVITS 的 MPS 訓練路徑較不穩定。
- 無 NVIDIA GPU：batch size 固定建議 `1`。
- 錄音少於 15 分鐘：SoVITS 建議 `4` epochs、GPT 建議 `10` epochs，先做 v0 smoke test。
- 錄音 15-45 分鐘：SoVITS 建議 `8` epochs、GPT 建議 `15` epochs。
- 錄音 45 分鐘以上：SoVITS 建議 `8` epochs、GPT 建議 `20` epochs。

如果訓練卡住或 Mac 發熱嚴重，先減 epochs，不要加 batch size。
