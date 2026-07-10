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

工具完成後，照 terminal 輸出的值填：

```text
Experiment/model name: <experiment>
Text labelling file:   <output>/<experiment>.list
Audio dataset folder:  leave blank
Language:              yue
```

因為 `.list` 已經使用 audio 的絕對路徑，`Audio dataset folder` 留空即可。
