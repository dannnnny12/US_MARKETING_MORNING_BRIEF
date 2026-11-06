import os
from flask import Flask, request
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)

@app.route("/callback", methods=['POST'])
def callback():
    body = request.get_json(silent=True)
    if body:
        events = body.get('events', [])
        for event in events:
            # 檢查是否為使用者傳送訊息的事件
            if event.get('type') == 'message':
                user_id = event['source'].get('userId')
                print(f"\n========================================")
                print(f"🔥 成功抓到 User ID: {user_id}")
                print(f"========================================\n")
                
    # ⚠️ 非常重要：不管 LINE 傳什麼來，最後一定要回傳 200 OK，才不會讓 LINE 報錯
    return 'OK', 200

if __name__ == "__main__":
    app.run(port=5000)