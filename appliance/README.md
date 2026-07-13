# Marksys 賽務專用機（Tier 1：穩陣雲端專用機）

呢個 `appliance/` 層將一部退役 laptop（例如 Lenovo T470s + Ubuntu LTS）變成賽務系統嘅
**專用機**。設計前提：

- 評判用**自己嘅電話/iPad**，開現有 **cloud URL**。
- 資料庫係 **Supabase (PostgreSQL)**。
- **場地網絡穩定、未斷過。**

所以呢部機**唔需要**做離線伺服器。佢嘅角色係：
**穩陣嘅雲端 client ＋ 顯示／控制／備份站**，完全唔改現有 app、schema、UI。

> 明確**唔做**（Tier 2 先考慮）：local PostgreSQL、雙向同步、`hostapd` WiFi 熱點、4G/5G uplink。

---

## 功能總覽

| 功能 | 狀態 | 說明 |
|---|---|---|
| 定時備份 Supabase | ✅ 已實作 | `backup_db.sh` + systemd timer，純保險 |
| 健康狀態（畫面燈） | ✅ 已實作 | 右上角綠／黃／紅燈：App OK、備份新鮮度、碟空間 |
| Kiosk（開機揀 mode） | ✅ 已實作 | 開機揀「日常練習」定「比賽日」 |
| 大屏投影（辯題/隊名/發言者） | ✅ 已實作 | 比賽日大 mon，唔顯示時間 |

---

## 已實作：定時備份

零風險，唔掂到 app。讀取同 app 一樣嘅 `[connections.postgresql]` 連線，`pg_dump` 落本機。

### 檔案
- `backup_db.sh` — 主備份腳本（可手動跑，亦畀 timer 跑）。
- `marksys-backup.service` / `marksys-backup.timer` — 每日 02:30 自動備份。
- `appliance.env.example` — 設定樣板。

### 安裝步驟（喺 Ubuntu 專用機上）

```bash
# 1. 安裝工具。postgresql-client 版本要 >= Supabase 嘅主版本（一般 16/17）。
sudo apt update
sudo apt install -y postgresql-client python3
# 讀 secrets 需要 TOML parser。Python 3.11+ 內置 tomllib（Ubuntu 24.04+ 已有）。
# 若用 Ubuntu 22.04（python3 = 3.10，無 tomllib），額外裝 tomli：
#   sudo apt install -y python3-tomli

# 2. 建立專用 user、目錄
sudo useradd -r -s /usr/sbin/nologin marksys || true
sudo mkdir -p /opt/skhlmc-dbt-marksys /etc/marksys /var/backups/marksys
sudo chown -R marksys:marksys /var/backups/marksys

# 3. 放 repo（或最少 appliance/）落 /opt/skhlmc-dbt-marksys
#    e.g. sudo git clone <repo> /opt/skhlmc-dbt-marksys

# 4. 放 secrets（可沿用現有 secrets.toml 內容）
sudo cp your-secrets.toml /etc/marksys/secrets.toml
sudo chown marksys:marksys /etc/marksys/secrets.toml
sudo chmod 600 /etc/marksys/secrets.toml

# 5. 設定
sudo cp /opt/skhlmc-dbt-marksys/appliance/appliance.env.example /etc/marksys/appliance.env
sudo nano /etc/marksys/appliance.env   # 需要時改 RETENTION_DAYS、USB_MOUNT

# 6. 先手動試一次
sudo -u marksys MARKSYS_ENV_FILE=/etc/marksys/appliance.env \
    /opt/skhlmc-dbt-marksys/appliance/backup_db.sh

# 7. 裝 systemd timer
sudo cp /opt/skhlmc-dbt-marksys/appliance/marksys-backup.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now marksys-backup.timer
```

### 日常操作

```bash
systemctl list-timers marksys-backup.timer      # 睇下次幾時跑
sudo systemctl start marksys-backup.service     # 比賽前手動備份一次
journalctl -u marksys-backup.service -f         # 睇 log
cat /var/backups/marksys/last_backup.status     # 最近一次結果（畀健康燈用）
```

### 還原（有需要時）

```bash
# custom format，用 pg_restore
pg_restore --no-owner --no-privileges -d "<target-connection>" \
    /var/backups/marksys/marksys-YYYYMMDD-HHMMSS.dump
```

> **Supabase 注意**：`pg_dump` 要用**直連／session 連線（port 5432）**。
> Transaction pooler（port 6543）唔支援 `pg_dump`，腳本偵測到會出 WARN。

---

## 已實作：健康燈（畫面狀態）

每分鐘檢查一次，寫 JSON，畫面右上角常駐一粒燈：

- 🟢 **OK** ／ 🟡 **WARN** ／ 🔴 **FAIL** ／ ⚫ 未有資料
- 檢查項：**App 開唔開得到**（cloud URL HTTP 200）、**最近備份新鮮度**、**碟剩餘空間**
- 顯示兩行文字：`App: OK   碟: 63% 剩` / `備份: OK (3小時前)`

> Tier 1：app 喺 cloud，冇法喺專用機數評判機連線，所以只查「app 通唔通、備份夠唔夠新、碟夠唔夠位」——呢啲先係主席／IT 要一眼睇到嘅嘢。

### 檔案
- `health_check.sh` — 做檢查，寫 `HEALTH_FILE`（預設 `/var/lib/marksys/health.json`）。
- `marksys-health.service` / `.timer` — 每分鐘跑一次。
- `health_overlay.py` — Tkinter 常駐視窗（由 kiosk session 起）。

### 安裝

```bash
# 依賴：curl、python3-tk
sudo apt install -y curl python3-tk
sudo mkdir -p /var/lib/marksys && sudo chown marksys:marksys /var/lib/marksys

# 手動試一次，睇 JSON 出唔出到
sudo -u marksys MARKSYS_ENV_FILE=/etc/marksys/appliance.env \
    /opt/skhlmc-dbt-marksys/appliance/health_check.sh
cat /var/lib/marksys/health.json

# 裝 timer
sudo cp /opt/skhlmc-dbt-marksys/appliance/marksys-health.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now marksys-health.timer
```

---

## 已實作：Kiosk 開機揀 mode

開機自動登入 → 揀模式 → Chromium kiosk。㩒 **Alt+F4** 退出 Chromium 會返去揀 mode，唔使重開機。

```
┌─────────────────────────────┐
│   聖呂中辯電子系統          │
│   揀模式：                   │
│   🟢  日常練習（學生/教練）  │
│   🔴  比賽日（主席）         │
└─────────────────────────────┘
```

- **日常練習** → 開 `PRACTICE_URL`（未設就用 `APP_URL`）
- **比賽日** → 開 `CONTEST_URL`（未設就用 `APP_URL`）
- 兩個 mode 都指去 cloud（Tier 1）。想佢哋開唔同 path，喺 `appliance.env` 設 `PRACTICE_URL`／`CONTEST_URL`。
- `CHOOSER_TIMEOUT>0` 可以設「無人揀就 N 秒後自動入日常練習」。

### 檔案
- `marksys-kiosk.sh` — 揀 mode + Chromium kiosk 迴圈。
- `xinitrc` — X session 入口（起 openbox + 健康燈 + kiosk）。

### 安裝

```bash
# 依賴：X、輕量 WM、Chromium、chooser、隱藏鼠標
sudo apt install -y xserver-xorg xinit openbox chromium-browser zenity unclutter
# 注意：部分 Ubuntu 版 chromium 係 snap，package 名或路徑可能係 chromium；
# marksys-kiosk.sh 會自動搵 chromium / chromium-browser / google-chrome。

# 1. 部署 X session 檔案畀 marksys user
#    marksys 之前用 nologin 建立；kiosk 要可登入，改返 shell：
sudo usermod -s /bin/bash marksys
sudo mkdir -p /home/marksys && sudo chown marksys:marksys /home/marksys
sudo cp /opt/skhlmc-dbt-marksys/appliance/xinitrc /home/marksys/.xinitrc
sudo chown marksys:marksys /home/marksys/.xinitrc

# 2. tty1 開機自動登入 marksys
sudo mkdir -p /etc/systemd/system/getty@tty1.service.d
sudo tee /etc/systemd/system/getty@tty1.service.d/autologin.conf >/dev/null <<'EOF'
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin marksys --noclear %I $TERM
EOF

# 3. 登入後自動開 X（放入 marksys 嘅 ~/.bash_profile）
sudo tee /home/marksys/.bash_profile >/dev/null <<'EOF'
if [ -z "$DISPLAY" ] && [ "$(tty)" = "/dev/tty1" ]; then
    exec startx
fi
EOF
sudo chown marksys:marksys /home/marksys/.bash_profile

sudo systemctl daemon-reload
# 重開機測試，或者切去 tty1
```

### 退出 / 除錯

```bash
# 退出 Chromium 返去 chooser：Alt+F4
# 完全退出圖形介面除錯：Ctrl+Alt+F2 切去另一個 tty 登入
journalctl -b | grep -i startx        # X 起唔起到
cat /var/lib/marksys/health.json      # 健康燈讀緊嘅資料
```

> 若 chromium 係 snap 版，kiosk 大致一樣，但個別 `--flag` 或 profile 路徑可能有出入，試機時留意 `journalctl`。

---

## 已實作：比賽日大屏投影

大 mon 顯示 **辯題、正反隊名、而家到邊個發言**（正反隊名會高亮邊邊發緊言，
發言者有名就埋名）。**刻意唔顯示時間** —— 時間仍然喺主席自己裝置嘅 app。

呢部分係喺 **app（proxy 層）** 加嘅新頁，唔喺 appliance 腳本度。兩條網址：

| 網址 | 畀邊個 | 需唔需要登入 |
|---|---|---|
| `<APP_URL>/projector` | 大屏顯示（比賽日 kiosk 開呢個） | 唔使 |
| `<APP_URL>/projector/control` | 主席／IT 控制 | 要，委員帳戶（同一瀏覽器先登入 app） |

**點運作**
- 控制頁揀場次（由 `matches` 讀辯題／正反隊）＋ 揀賽制 → 撳「套用場次」。
- 撳「顯示大屏」開／關投影內容；「下一位／上一位」或撳進程列表推進發言者。
- 大屏每 2 秒 poll 一次，自動跟住更新。

**比賽日建議接法**
- 部機 HDMI 出投影機；比賽日 kiosk 設 `CONTEST_URL=<APP_URL>/projector`（見 `appliance.env`）。
- 主席／IT 用自己電話／平板開 `<APP_URL>/projector/control`（先登入委員帳戶）。
- 想控制同顯示都喺部機一齊做，可以喺同一部機開兩個 Chromium 視窗（一個 display 拉去投影屏，一個 control 喺 laptop 屏）。

> 技術上：新增 `deploy/proxy.py` 幾條 route（排喺 catch-all 之前）＋ `templates/projector_display.html`、`templates/projector_control.html`，同一張自動建立嘅 `projector_state` 表。只**讀** `matches`／`debaters`，唔改任何現有表或頁。
