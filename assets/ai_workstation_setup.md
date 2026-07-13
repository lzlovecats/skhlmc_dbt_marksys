# 聖呂中辯 AI Workstation 操作手冊

本手冊將 RTX 3060 Desktop 設定成「日常 Linux 桌面＋隔離 AI 訓練及測試推理」工作站。標準系統為 **Pop!_OS 24.04 LTS NVIDIA ISO**；正式網站不會直接連入家中電腦。

## 1. 安裝前備份及盤點

1. 將 EndeavourOS 個人資料、SSH key、Git 設定、瀏覽器資料及未提交 repo 複製到加密外置碟。
2. 另存以下輸出，方便重建：

   ```bash
   lsblk -f
   lspci -nnk | grep -A3 -E 'VGA|3D'
   nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv
   pacman -Qqe > endeavour-packages.txt
   ```

3. 從 [System76](https://system76.com/pop/download/) 下載 Pop!_OS 24.04 LTS **with NVIDIA** ISO，以下載頁列出嘅 SHA-256 核對：

   ```bash
   sha256sum pop-os_24.04_amd64_nvidia_*.iso
   ```

4. 寫入 USB 前再次確認目標裝置；下例嘅 `/dev/sdX` 必須換成 USB，唔可以係系統碟：

   ```bash
   sudo dd if=pop-os_24.04_amd64_nvidia_*.iso of=/dev/sdX bs=4M status=progress oflag=sync
   ```

5. 用 live USB 測試網絡、聲音、休眠及 NVIDIA 顯示後先安裝。安裝時啟用 full-disk encryption。

成功判斷：重啟後可以正常登入，`nvidia-smi` 顯示 RTX 3060、VRAM及driver，而非 `Nouveau`。

## 2. 建立隔離帳戶及儲存區

日常帳戶保留 `sudo`；訓練帳戶無 `sudo`：

```bash
sudo adduser ai-train
sudo install -d -m 700 -o ai-train -g ai-train /srv/ai
sudo -u ai-train mkdir -p /srv/ai/{datasets/{raw,snapshots},models/{base,checkpoints,releases},logs,container-data,backups,src}
sudo chmod -R go-rwx /srv/ai
```

日常帳戶要操作訓練環境時使用 `sudo -iu ai-train`。模型、錄音、API key及dataset不可放入網站repo，亦不可放入可公開同步嘅雲端資料夾。

成功判斷：其他普通帳戶執行 `ls /srv/ai`會得到 `Permission denied`；`ai-train`可以建立檔案。

## 3. 記憶體及桌面可用性

目前16GB RAM只用於 GPT-SoVITS及小型推理。開始7B QLoRA前升級至32GB。

```bash
free -h
swapon --show
```

確保至少有16GB swap／zram。訓練 container預設 `mem_limit: 12g`、`shm_size: 4g`；長時間工作以較低優先次序啟動：

```bash
nice -n 10 ionice -c 3 docker compose run --service-ports GPT-SoVITS-CU126-Lite
```

若桌面出現明顯交換記憶體、音訊斷續或OOM，停止訓練，降低batch size，唔好取消memory limit硬頂。

## 4. Rootless Docker及NVIDIA runtime

依照 [Docker Ubuntu安裝指引](https://docs.docker.com/engine/install/ubuntu/) 安裝 Docker Engine、CLI及 `docker-ce-rootless-extras`，再安裝rootless先決條件：

```bash
sudo apt update
sudo apt install -y uidmap dbus-user-session curl ca-certificates gnupg2
sudo loginctl enable-linger ai-train
sudo -iu ai-train
dockerd-rootless-setuptool.sh install
systemctl --user enable --now docker
```

將安裝程式顯示嘅 `DOCKER_HOST`及PATH加入 `/home/ai-train/.profile`，重新登入後確認：

```bash
docker info | grep -i rootless
```

按 [NVIDIA Container Toolkit官方apt及rootless步驟](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html) 安裝production repository及toolkit。日常sudo帳戶完成套件安裝後，切回 `ai-train`：

```bash
nvidia-ctk runtime configure --runtime=docker --config="$HOME/.config/docker/daemon.json"
systemctl --user restart docker
```

日常sudo帳戶只需執行一次NVIDIA rootless cgroup設定：

```bash
sudo nvidia-ctk config --set nvidia-container-cli.no-cgroups --in-place
```

GPU smoke test：

```bash
docker run --rm --gpus all nvidia/cuda:12.6.3-base-ubuntu24.04 nvidia-smi
docker run --rm --gpus all pytorch/pytorch:2.7.0-cuda12.6-cudnn9-runtime \
  python -c 'import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0), torch.cuda.get_device_properties(0).total_memory)'
```

成功判斷：兩個container均見到同一張RTX 3060，PyTorch第一個值係`True`。

## 5. GPT-SoVITS固定環境

```bash
sudo -iu ai-train
cd /srv/ai/src
git clone https://github.com/RVC-Boss/GPT-SoVITS.git
cd GPT-SoVITS
git rev-parse HEAD | tee /srv/ai/logs/gpt-sovits-commit.txt
docker compose pull GPT-SoVITS-CU126-Lite
docker image inspect "$(docker compose images -q GPT-SoVITS-CU126-Lite)" \
  --format '{{index .RepoDigests 0}}' | tee /srv/ai/logs/gpt-sovits-image.txt
```

建立只供本機使用嘅 `compose.override.yaml`；除dataset／model mount外，所有port必須bind `127.0.0.1`：

```yaml
services:
  GPT-SoVITS-CU126-Lite:
    ports:
      - "127.0.0.1:9874:9874"
    volumes:
      - /srv/ai/datasets:/workspace/datasets
      - /srv/ai/models:/workspace/models
      - /srv/ai/logs:/workspace/logs
    mem_limit: 12g
    shm_size: 4g
```

啟動：

```bash
docker compose run --service-ports GPT-SoVITS-CU126-Lite
ss -lntp | grep 9874
```

成功判斷：瀏覽器可開 `http://127.0.0.1:9874`，而 `ss`只顯示`127.0.0.1:9874`，唔係`0.0.0.0`。

## 6. Dataset準備、訓練及resume

1. 從AI Training管理頁按單一speaker下載accepted ZIP，保存到 `/srv/ai/datasets/raw`。
2. 在網站repo版本對應嘅工具環境執行：

   ```bash
   python3 tools/prepare_gpt_sovits_dataset.py \
     /srv/ai/datasets/raw/tts-accepted.zip \
     --speaker SPEAKER_ID \
     --output-dir /srv/ai/datasets/snapshots/tts-v0
   ```

3. 檢查 `snapshot_manifest.json`、`quality_report.json`、train／validation／test list及normalized WAV。
4. WebUI先做dataset formatting，再以 fp16、batch 1訓練；RTX 3060 ≥10GB而RAM穩定先試batch 2。
5. 每次實驗建立獨立目錄，記錄snapshot ID、Git commit、container digest、參數及開始時間。
6. Resume時必須沿用同一snapshot同config；唔可以將新錄音直接混入舊run。

成功判斷：訓練可以完成一個epoch、GPU有負載、冇OOM，validation loss及checkpoint寫入指定實驗目錄。

## 7. 評估及本地推理

- 固定test split不可加入訓練。
- 產生100句讀音測試、保存ASR CER、讀音正確率、MOS、speaker consistency、first-audio latency。
- 只喺四項TTS deployable指標齊備後登記candidate：`cer`、`mos`、`pronunciation_accuracy`、`first_audio_ms`。
- 本地API只bind loopback，例如：

  ```bash
  python api_v2.py -a 127.0.0.1 -p 9880 -c GPT_SoVITS/configs/tts_infer.yaml
  ss -lntp | grep 9880
  ```

- 正式Render網站唔可以使用`127.0.0.1`連家中Desktop；正式custom TTS要另行部署受認證GPU endpoint。

## 8. 備份、保留及更新

- 每次ready snapshot及deployable model複製到加密外置碟；核對SHA-256後先標記備份完成。
- 每個run只保留`best`、`last`同正式release；其餘checkpoint確認無引用後刪除。
- 每月檢查 `/srv/ai`容量：

  ```bash
  df -h /srv/ai
  du -h --max-depth=2 /srv/ai | sort -h | tail -30
  ```

- 更新Pop!_OS／NVIDIA driver前先完成checkpoint、停止container及備份；更新後重跑兩個GPU smoke tests。
- 唔好在可resume訓練途中更新driver、container image或GPT-SoVITS commit。

## 9. 故障復原

- `nvidia-smi`失敗：先重啟；再用Pop!_OS NVIDIA套件重裝driver，唔好混用NVIDIA `.run` installer。
- Host見GPU、container見唔到：重跑rootless `nvidia-ctk runtime configure`，restart user Docker，核對`no-cgroups`。
- CUDA OOM：batch降至1、確認fp16、停止其他GPU程式；唔好將test資料刪除換取空間。
- 系統OOM：停止run、保留last checkpoint，檢查swap及container memory；LLM工作延至升32GB。
- Dataset撤回：停止所有引用該snapshot嘅checkpoint，重新匯出、建立新snapshot及重訓，舊artifact標記blocked。

## 10. 最終安全清單

- [ ] `/srv/ai`權限係700，訓練資料不在Git。
- [ ] `ai-train`無sudo，Docker為rootless。
- [ ] 所有WebUI／API只listen `127.0.0.1`。
- [ ] GPU smoke test通過並記錄VRAM；低於10GB只做TTS及3B／4B LLM。
- [ ] 7B QLoRA前已升32GB RAM。
- [ ] 每個模型有dataset snapshot、config、metrics及artifact hash。
- [ ] 撤回資料對應嘅snapshot／checkpoint已blocked。
