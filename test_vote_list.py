from functions import execute_query
import traceback

def test():
    try:
        query = "UPDATE topic_votes SET against_users=:new_against_str, agree_users=:new_agree_str WHERE topic=:topic"
        param = {"new_against_str": ["a", "b"], "new_agree_str": ["c", "d"], "topic": "testing"}
        execute_query(query, param)
        print("Success")
    except Exception as e:
        print(f"FAILED: {type(e)}")
        traceback.print_exc()

if __name__ == '__main__':
    test()
