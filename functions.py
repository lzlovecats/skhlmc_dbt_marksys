import streamlit as st
import gspread
import json
import pandas as pd
import random
from google.oauth2.service_account import Credentials

SCOPES = ["https://www.googleapis.com/auth/spreadsheets",  "https://www.googleapis.com/auth/drive"]

def check_admin():
    if "admin_logged_in" not in st.session_state:
        st.session_state["admin_logged_in"] = False
        
    if not st.session_state["admin_logged_in"]:
        st.subheader("賽會人員登入")
        pwd = st.text_input("請輸入賽會人員密碼", type="password")
        if st.button("登入"):
            if pwd == st.secrets["admin_password"]:
                st.session_state["admin_logged_in"] = True
                st.rerun()
            else:
                st.error("密碼錯誤")
        return False
    return True
    
def check_score():
    if "score_logged_in" not in st.session_state:
        st.session_state["score_logged_in"] = False
        
    if not st.session_state["score_logged_in"]:
        st.subheader("查閱比賽分紙登入")
        pwd = st.text_input("請輸入由賽會人員提供的密碼", type="password")
        if st.button("登入"):
            if pwd == st.secrets["score_password"]:
                st.session_state["score_logged_in"] = True
                st.rerun()
            else:
                st.error("密碼錯誤")
        return False
    return True
    
def get_connection():
    creds = Credentials.from_service_account_info(
        st.secrets["gcp_service_account"],
        scopes=SCOPES
    )
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key("1y8FFMVfp1to5iIVAhNUPvICr__REwslUJsr_TkK3QF8")
    return spreadsheet

def load_data_from_gsheet():
    try:
        spreadsheet = get_connection()
        sheet = spreadsheet.worksheet("Match")
        records = sheet.get_all_records()
        
        data_dict = {}
        for row in records:
            m_id = str(row["match_id"])
            if m_id:
                data_dict[m_id] = row
        return data_dict
    except Exception as e:
        st.error(f"連線錯誤: {e}")
        return {}

def save_match_to_gsheet(match_data):
    spreadsheet = get_connection()
    sheet = spreadsheet.worksheet("Match")
    try:
        match_ids = sheet.col_values(1)
        
        row_values = [
            match_data["match_id"],
            str(match_data["date"]),
            str(match_data["time"]),
            match_data["que"],
            match_data["pro"],
            match_data["con"],
            match_data["pro_1"], match_data["pro_2"], match_data["pro_3"], match_data["pro_4"],
            match_data["con_1"], match_data["con_2"], match_data["con_3"], match_data["con_4"], match_data.get("access_code", "")
        ]

        if match_data["match_id"] in match_ids:
            row_index = match_ids.index(match_data["match_id"]) + 1
            st.info("更新舊有紀錄中，請稍等。")
            sheet.delete_rows(row_index)
            sheet.append_row(row_values)
        else:
            sheet.append_row(row_values)
        
    except Exception as e:
        st.error(f"寫入失敗: {e}")
        
def delete_match_from_gsheet(match_id):
    spreadsheet = get_connection()
    sheet = spreadsheet.worksheet("Match")

def save_draft_to_gsheet(match_id, judge_name, team_side, score_data):
    try:
        spreadsheet = get_connection()
        worksheet = spreadsheet.worksheet("Temp")

        data_to_save = score_data.copy()
        
        if "raw_df_a" in data_to_save:
            data_to_save["raw_df_a"] = data_to_save["raw_df_a"].to_json()
        if "raw_df_b" in data_to_save:
            data_to_save["raw_df_b"] = data_to_save["raw_df_b"].to_json()
            
        json_str = json.dumps(data_to_save, ensure_ascii=False)

        # Find and delete all existing drafts for this specific judge/match/side
        all_values = worksheet.get_all_values()
        rows_to_delete = []
        for i, row in enumerate(all_values):
            if i == 0: continue  # Skip header
            if (len(row) >= 3 and
                str(row[0]) == str(match_id) and
                str(row[1]) == str(judge_name) and
                str(row[2]) == str(team_side)):
                rows_to_delete.append(i + 1)

        if rows_to_delete:
            for row_num in sorted(rows_to_delete, reverse=True):
                worksheet.delete_rows(row_num)

        # Append the new, updated draft
        worksheet.append_row([str(match_id), str(judge_name), str(team_side), json_str])
            
        return True
    except Exception as e:
        st.error(f"無法上傳暫存資料至Google Cloud: {e}")
        return False
    
def load_draft_from_gsheet(match_id, judge_name):
    try:
        spreadsheet = get_connection()
        worksheet = spreadsheet.worksheet("Temp")
            
        all_values = worksheet.get_all_values()
        result = {"正方": None, "反方": None}
        
        for i, row in enumerate(all_values):
            if i == 0: continue  # Skip header
            if len(row) < 4: continue # Ensure row has enough columns
            
            if (str(row[0]) == str(match_id) and 
                str(row[1]) == str(judge_name)):
                
                side = row[2]
                json_str = row[3]
                
                if json_str:
                    try:
                        data = json.loads(json_str)
                        if "raw_df_a" in data:
                            data["raw_df_a"] = pd.read_json(data["raw_df_a"])
                        if "raw_df_b" in data:
                            data["raw_df_b"] = pd.read_json(data["raw_df_b"])
                        result[side] = data
                    except:
                        pass
        return result
    except Exception as e:
        return {"正方": None, "反方": None}

def load_topic_from_gsheet():
    try:
        spreadsheet = get_connection()
        sheet = spreadsheet.worksheet("Topic")
        all_records = sheet.get_all_records()
        
        # Extract the topic from each record, filtering out any empty rows/values.
        # get_all_records() does not include the header row.
        topics = [row["topic"] for row in all_records if row.get("topic")]
        return topics
    except Exception as e:
        st.error(f"連線錯誤: {e}")
        return []
    
def draw_a_topic():
    all_topic = load_topic_from_gsheet()
    if all_topic:
        return random.choice(all_topic)
    else:
        st.error("抽取辯題失敗：辯題庫為空或出現錯誤。")
        return ""

def draw_pro_con(team1, team2):
    t_list = []
    draw_num = random.randint(0, 1)
    if draw_num == 0:
        t_list.append(team1)
        t_list.append(team2)
    elif draw_num == 1:
        t_list.append(team2)
        t_list.append(team1)
    return t_list

def return_user_manual():
    manual_content = """
    本系統共分為三個主要使用介面：評判、賽會人員 及 比賽隊伍。請根據你的身份查看對應章節。

    ### 一、評判
    #### A. 登入系統
    - 在左側選單選擇 「電子分紙（評判用）」。
    - 在下拉選單中選擇正確的 比賽場次。
    - 輸入由賽會提供的 入場密碼 (Access Code)，點擊「驗證入場」。

    #### B. 填寫評分表
    - **輸入姓名**：
      - 請務必輸入你的中文全名。
      - ⚠️ **重要提示**：系統會根據此姓名讀取雲端暫存檔。若不慎重新整理網頁，只需重新輸入完全相同的姓名，系統會自動恢復你之前的評分資料。

    - **選擇評分隊伍**：
      - 系統分為「正方」與「反方」。請先選擇其中一方（例如先評正方）。
    - **評分項目**：
      - **（甲）台上發言**：輸入四位辯員的內容、辭鋒、組織、風度分數。每個欄位填寫一個整數(1-10)，系統會自動計算總分。
      - **（乙）自由辯論**：輸入該隊的整體分數。系統會自動計算總分。
      - **（丙）扣分及內容連貫**：輸入扣分總和及內容連貫分數。
      - 下方會顯示該方目前的總分。

    #### C. 暫存與切換 (關鍵步驟)
    為了防止數據遺失及進行雙方評分，請嚴格遵守以下流程：

    1. 完成一方（例如正方）評分後，必須點擊下方的 「暫存正方評分」 按鈕。
       - **注意**：若有細項為 0 分，系統會彈出警告，但仍可暫存。
    2. 看見「已暫存正方分數」的提示後，在上方「選擇評分隊伍」切換至 「反方」。
    3. 完成反方評分後，點擊 「暫存反方評分」。

    #### D. 正式提交
    當正、反雙方的評分進度都顯示為 「已暫存 ☑️」 時，頁面最下方會出現 「正式提交評分」 的紅色按鈕。

    - 確認所有分數無誤後，點擊提交。
    - ⚠️ **警告**：評分一旦正式提交，即會上傳至賽會資料庫，無法再次修改。


    ### 二、賽會人員
    #### A. 登入管理後台
    賽會人員擁有三個管理頁面，均需輸入管理員密碼：

    - **比賽場次管理**：設定賽程、辯題、辯員。
    - **查閱比賽結果**：查看賽果、最佳辯論員。
    - **辯題庫管理**：新增或刪除辯題。

    #### B. 建立與管理場次
    這是賽會最常用的功能，請在比賽開始前完成設定。

    - **新增場次**：
      - 在上方輸入「比賽場次編號」（例如：第一屆初賽），點擊「新增比賽場次」。

    - **編輯場次資料**：
      - 選擇場次後，可設定日期、時間、辯題。
      - **隊伍與辯員**：請務必填寫正反方隊名及辯員姓名，這些資料會直接顯示在評判的分紙上。
      - **評判入場密碼**：在此欄位設定密碼（Access Code），評判需憑此碼登入該場次。

    - **抽籤功能**：
      - **抽辯題**：點擊「抽辯題」可從資料庫隨機抽取一條題目。
      - **抽站方**：點擊「抽站方」，輸入兩隊名稱，系統會隨機分配正反方。

    - **刪除場次**：
      - ⚠️ **危險操作**：刪除場次會連帶刪除該場次的所有評判評分紀錄及暫存檔，且無法復原。

    #### C. 查閱賽果
    當評判提交分數後，此頁面會即時更新。

    - **勝負判定**：系統會統計正方票數、反方票數及平票數，自動判斷勝方。
      - **注意**：若票數相同，系統會提示需進行自由辯論重賽（依賽規）。

    - **最佳辯論員統計**：
      - 系統計算邏輯：優先比較 「名次總和」 (數值越小越好)，若名次相同則比較 「平均得分」。
      - 列表會顯示所有辯員的排名數據，排第一位者即為本場最佳辯論員。

    #### D. 辯題庫管理
    - **新增**：輸入題目後上傳。系統會自動檢查是否重複。
    - **刪除**：可多選辯題進行刪除。

    ### 三、比賽隊伍
    #### A. 查閱分紙
    - 進入 「查閱比賽分紙（比賽隊伍用）」 頁面。
    - 輸入由賽會人員提供的 查卷密碼（注意：此密碼通常與賽會管理員密碼不同，由賽會決定何時公佈）。

    #### B. 查看詳情
    - **選擇場次**：選取你們參賽的場次。
    - **選擇評判**：系統會列出該場次所有已提交分數的評判。
    - **閱讀評分**：你可以看到該位評判對雙方的完整評分，內容包括：
      - 甲部：每位辯員的細項得分。
      - 乙部：自由辯論得分。
      - 扣分與連貫性。
      - 總分。
        """
    return manual_content

def return_rules():
    rules = """
        ### 重要提示：此文件為人工智能根據賽規原文生成，僅供參考。如有任何爭議，以賽會內部的完整賽規原文作裁決。比賽隊伍可向賽會索取完整賽規。

        ---

        ### 零、引言
        聖公會呂明才中學中文辯論隊（下稱聖呂中辯）作為賽會，擁有對此賽規的最終釋義權。
        * 在本章中，除文意另有所指外，「辯員」指所有作賽時的辯員。
        * 在本章中，除文意另有所指外，「主席」指比賽作賽期間負責賽事流程的賽事主席。

        ### 一、比賽方式
        1.  比賽採用 **4-3-3-4** 之賽制。每場比賽雙方可選擇派出一至四位辯員出賽，同一人可以同時擔任多個辯位。
        2.  比賽設有自由辯論環節，設於反方二副發言後，每隊 **兩分半鐘**。

        ### 二、比賽細則
        1.  對賽雙方的辯員須於 **比賽開始前 10 分鐘** 抵達作賽場地並出席比賽。如參賽隊伍未能於比賽開始時間前到達比賽場地，參賽隊伍會被視作棄權。上述時間以香港天文台為準。
        
        2.  於每場比賽前，主席須向在場人士解釋比賽之規則，若雙方辯員對賽制有任何疑問，須於比賽前提出，所有疑問若在比賽開始後提出，恕不受理。
        
        3.  **辯題與否決權**：比賽辯題將於比賽日期前 14 日當日下午五時傳送給對賽雙方。雙方各有一次辯題否決權，可於辯題發放後翌日下午四時前向賽會表示否決辯題。
            * 第一條辯題一經否決，第二條辯題將於比賽日期前 13 日發放。
            * 此時未行使否決權的一方可選擇是否否決第二條辯題（時限為翌日下午四時前）。
            * 如決定否決，第三條辯題（最終辯題）將於比賽日期前 12 日發放。
        4.  **抽籤**：比賽辯題均以抽籤決定。比賽站方則會於辯題確認後當日下午六時抽籤決定。賽會以內部 Python 程式進行抽籤（程式已開源）。
        
        5.  **語言**：比賽必需使用 **粵語**。書名、人名及專有名詞可使用外語。如違規使用外語，主席將通知評判於分紙上酌量扣分。
        
        6.  **行為規範**：所有辯員發言時不得：
            * (1) 對任何人作人身攻擊；
            * (2) 使用粗言穢語；或
            * (3) 作任何具冒犯性的行為。
            * *違規處理*：主席可提醒或直接終止該辯員發言。
            
        7.  **溝通限制**：台上辯論員嚴禁與台下觀眾進行任何形式的溝通，違例者將立即被取消參賽資格。
        
        8.  **違禁設備**：辯員在台上發言或比賽進行期間，**不得攜帶**任何具備以下功能的設備：
            * 顯示時間或計時功能；
            * 通訊功能；
            * 錄音或攝影功能。
            * *違規處理*：主席有權勒令移除，並通知評判扣分；如涉及台下溝通則直接取消資格。
            
        9.  **允許物品**：辯員只可攜帶文具、尺寸不大於 5x3 英寸的辯卡、或白紙。其他物品一律禁止。
        
        10. **計時失誤**：若工作人員計時失誤，經主席批准後，受影響辯員可重新發言或補足時間。
        
        11. **名單提交**：需於比賽日期前 7 日或之前提交。
            * 名單一經提交不接受修改，逾時提交將面臨扣分。
            * 不得使用另一參賽隊伍的辯員。
            
        12. **名單變動**：
            * 嚴禁名單以外人士上台，違者取消資格。
            * 如遇突發情況（如缺席），可由名單內其他辯員臨時頂替或兼任，但需按第六節第五條進行扣分。

        ### 三、發言次序及時間
        1.  **次序**：正方主辯 -> 反方主辯 -> 正方一副 -> ... -> 自由辯論 -> 反方結辯 -> 正方結辯。
        
        2.  **時間**：
            * 主辯：4 分鐘
            * 副辯：3 分鐘
            * 結辯：4 分鐘
            
        3.  **自由辯論**：
            * 設於反方二副發言後。
            * 每隊 **2.5 分鐘**。
            * 每次只可派 **一位** 辯員發言。
            * 當一方用盡時間後，另一方需派出一位辯員將剩餘時間用盡（主席不告知剩餘時間，僅有鐘聲提示）。
            * 自由辯論不設緩衝時間。

        ### 四、計時制度
        以計時員碼表為準。鳴鐘示意如下：
        * 🔔 **1 次**：發言時限前 30 秒。
        * 🔔🔔 **2 次**：發言時限屆滿（正鐘）。
        * 🔔🔔🔔 **3 次**：緩衝時間（15秒）屆滿。
        * 🔔🔔🔔🔔🔔 **5 次**：緩衝時間後逾時 25 秒（必須停止發言）。
        * *註：發言時間完畢後第 40.01 秒，主席須勒令停止發言。*

        ### 五、評分方式

        **1. 台上發言評分 (每位辯員 100 分)**

        | 項目 | 分數 | 備註 |
        | :--- | :--- | :--- |
        | 內容 | 40 | |
        | 辭鋒 | 30 | |
        | 組織 | 20 | |
        | 風度 | 10 | |
        | **總分** | **100** | |
        | 內容連貫 | 5 | 全隊共用，評估辯位間連貫度 |

        **2. 自由辯論評分 (全隊 55 分)**

        | 項目 | 分數 |
        | :--- | :--- |
        | 內容 | 20 |
        | 辭鋒 | 15 |
        | 組織 | 10 |
        | 風度 | 5 |
        | 合作 | 5 |
        | **總分** | **55** |

        **3. 隊伍總分計算**
        * 總分 = (4 位辯員台上發言) + (自由辯論) + (內容連貫) - (扣分)
        * 滿分為 **460 分**。

        **4. 最佳辯論員**
        * 從兩隊所有台上辯員中選出。
        * **評判標準**：
            1.  **名次總和**（數值越低越好）：統計所有評判給予該辯員之名次加總。
            2.  **平均得分**（如名次總和相同）：平均分較高者勝。

        **5. 勝負判定**
        * 一位評判：總分高者勝。
        * 多位評判：**票數多者勝**。
        * **平票**：經同意後增設自由辯論環節（重賽該環節），設 2 分鐘準備時間。

        ### 六、扣分制度

        1.  **逾時扣分**
            * 從發言時間完畢後第 **15.01 秒** 開始計算。
            * 每逾時 5 秒扣 **3 分**（不足 5 秒亦作 5 秒計）。
            * 上限：扣 **15 分**（即逾時 25 秒）。
        2.  **外語扣分**
            * 非專有名詞之使用，每次酌量扣 **1-5 分**。
        3.  **遲交名單扣分**

        | 延遲提交日數 | 所扣除之分數 |
        | :--- | :--- |
        | 1 日 | 2 分 |
        | 2 日 | 4 分 |
        | 3 日 | 7 分 |
        | 4 日或以上 | 8 分 |

        4.  **名單變動扣分**
            * 擔任非提交名單所載之辯位（臨時頂替/兼任），每個更動辯位扣 **5 分**。
        5.  **其他違規**
            * 最低扣 1 分，每一事項上限 8 分。

        ### 七、賽會人員職責
        * **主席**：負責流程、解釋賽規、宣讀辯題、對違規（遲到、外語、人身攻擊等）作判決及勒令停止發言。
        * **計時員**：操作官方碼表、鳴鐘、記錄時間（含自由辯論雙方時間）。
        * **評判**：根據四大範疇評分、評估連貫性及合作性、按主席通知執行扣分。
        """
    return rules
