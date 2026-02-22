import sys
import logging
from functions import execute_query
import traceback

logging.basicConfig(stream=sys.stdout, level=logging.DEBUG)

def test():
    try:
        query = "UPDATE topic_votes SET against_users=:new_against_str, agree_users=:new_agree_str WHERE topic=:topic"
        param = {"new_against_str": "a,b", "new_agree_str": "c,d", "topic": "1"}
        execute_query(query, param)
        print("Success")
    except Exception as e:
        print(type(e), e)
        traceback.print_exc()

test()
