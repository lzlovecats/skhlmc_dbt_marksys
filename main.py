import streamlit as st
import numpy as np
import pandas as pd

page_match_mgmt = st.Page("match_info.py", title="賽事資料管理系統（賽會人員用）")
page_judging = st.Page("judging.py", title="電子分紙（評判用）")
page_mgmt = st.Page("management.py", title="場次管理（賽會人員用）")

pg = st.navigation([page_match_mgmt, page_judging, page_mgmt])
st.set_page_config(page_title="電子分紙系統（Beta）", layout="wide")
pg.run()

#好，跟住落嚟就要整電子分紙「評判用」嘅框架，請你先教我搭一個table出嚟，模仿現有分紙