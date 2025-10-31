import time
import threading
import uuid
import os
from flask import Flask, render_template, jsonify, request, redirect, url_for, session, send_from_directory
from flask_socketio import SocketIO, emit, join_room
from playwright.sync_api import sync_playwright
from werkzeug.utils import secure_filename

# ----------------------------
# Flask App Setup
# ----------------------------
app = Flask(__name__)
app.secret_key = "luogu-duels-secret"
app.config["AVATAR_FOLDER"] = "static/avatars"
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16MB max file size
os.makedirs(app.config["AVATAR_FOLDER"], exist_ok=True)

socketio = SocketIO(app, cors_allowed_origins="*")

# ----------------------------
# Global State (Memory-based)
# ----------------------------
users = {}  # user_id -> {luogu_name, avatar}
rooms = {}  # room_id -> Room object

class Room:
    def __init__(self, room_id):
        self.room_id = room_id
        self.problems = set(["P1000"])
        self.teams = {"team1": [], "team2": []}
        self.members = set()
        # 修改：初始化分数为 0
        self.scores = {"team1": 0, "team2": 0}
        self.solved = set()
        # 新增：记录题目由谁解出
        self.solved_by = {} # {pid: {"user": username, "team": team_name}}
        self.finished = False
        self.winner = None
        self.created_at = time.time()
        self.proposals = []
        self.deletion_proposals = []

    def add_member(self, team, luogu_name):
        if team not in ["team1", "team2"]:
            return False
        if luogu_name in self.members:
            return False
        self.teams[team].append(luogu_name)
        self.members.add(luogu_name)
        # 无需再次初始化分数，已在 __init__ 中设置
        return True

    def remove_member(self, luogu_name):
        for team_name, members in self.teams.items():
            if luogu_name in members:
                members.remove(luogu_name)
                self.members.discard(luogu_name)
                return True
        return False

    def get_status(self):
        return {
            "room_id": self.room_id,
            "problems": list(self.problems),
            "teams": {k: v[:] for k, v in self.teams.items()},
            "solved": list(self.solved),
            # 修改：发送 solved_by 信息
            "solved_by": self.solved_by.copy(),
            "scores": self.scores.copy(),
            "finished": self.finished,
            "winner": self.winner,
            "proposals": self.proposals[:],
            "deletion_proposals": self.deletion_proposals[:]
        }

def fetch_ac_users_for_room(pid: str, room_members: set):
    url = f"https://www.luogu.com.cn/record/list?pid={pid}"
    print(f"[INFO] Fetching AC users for {pid} (room members: {len(room_members)}) ...")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context()
            context.add_cookies([
                {"name": "_uid", "value": "661094", "domain": "www.luogu.com.cn", "path": "/"},
                {"name": "__client_id", "value": "80b4a27bc7d95af2513b252879973a2f26a22f2c", "domain": "www.luogu.com.cn", "path": "/"}
            ])

            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=7000)
            page.wait_for_timeout(1000)

            # 修改：返回一个字典，key 是 pid，value 是 AC 的用户集合
            ac_by_pid = {pid: set()}
            rows = page.query_selector_all("div.row")
            for row in rows:
                status_span = row.query_selector("span.status-name")
                if not status_span:
                    continue
                status_text = status_span.inner_text().strip()
                if status_text != "Accepted":
                    continue

                user_span = row.query_selector(".user div > span > span > span > a > span")
                if user_span:
                    username = user_span.inner_text().strip()
                    if username in room_members:
                        ac_by_pid[pid].add(username)

            browser.close()
            print(f"[DEBUG] AC users for {pid} in room: {ac_by_pid[pid]}")
            return ac_by_pid

    except Exception as e:
        print(f"[ERROR] Failed to fetch AC users for {pid}: {e}")
        return {pid: set()}

# ----------------------------
# Judge Loop (Updated win condition)
# Win condition: First team to have any of its members solve ALL problems in the room wins
# Also ends if all problems are deleted (though unlikely)
# ----------------------------
def judge_room(room_id):
    room = rooms[room_id]
    print(f"[DEBUG] Judge loop started for room {room_id}")
    while not room.finished:
        ac_results = {} # 收集本次检查的所有 AC 结果
        for pid in list(room.problems):
            if pid in room.solved:
                continue # 跳过已解题目

            ac_users_for_pid = fetch_ac_users_for_room(pid, room.members)
            # ac_users_for_pid is {pid: {user1, user2, ...}}
            if pid in ac_users_for_pid:
                ac_results[pid] = ac_users_for_pid[pid]

        # 遍历收集到的 AC 结果
        for pid, ac_users in ac_results.items():
            if pid in room.solved: # 再次检查，防止并发问题
                continue

            solved_by_team = None
            for team_name in ["team1", "team2"]:
                if any(user in ac_users for user in room.teams[team_name]):
                    solved_by_team = team_name
                    break

            if not solved_by_team:
                continue # AC 的用户不在房间内队伍里

            # 标记题目为已解，记录解题者
            room.solved.add(pid)
            # 找到具体是哪个用户解的题 (从 AC 用户中找到属于该队伍的)
            solving_user = next(user for user in ac_users if user in room.teams[solved_by_team])
            room.solved_by[pid] = {"user": solving_user, "team": solved_by_team}
            # 更新分数
            room.scores[solved_by_team] += 100 # 假设每题100分
            print(f"[DEBUG] Room {room_id}: {solved_by_team} ({solving_user}) solved {pid}")

            # 修改：检查新的胜利条件 - 分数严格超过一半
            total_points = len(room.problems) * 100
            win_points = total_points // 2 # 例如 3题共300分，win_points = 150
            if room.scores[solved_by_team] > win_points: # 严格大于
                room.winner = solved_by_team
                room.finished = True
                print(f"[DEBUG] Room {room_id} FINISHED! Winner: {solved_by_team} (Score: {room.scores[solved_by_team]} > {win_points})")
                socketio.emit("game_over", {"winner": solved_by_team}, room=room_id)
                break # 退出 for 循环

            # 发送更新
            socketio.emit("update", room.get_status(), room=room_id)

        if not room.finished:
            time.sleep(10)
    print(f"[DEBUG] Judge loop ended for room {room_id}")


def get_current_user():
    uid = session.get("user_id")
    return users.get(uid) if uid else None

# ----------------------------
# Routes
# ----------------------------

@app.route("/")
def index():
    user = get_current_user()
    if not user:
        return redirect(url_for("register"))

    room_list = []
    for rid, room in rooms.items():
        # Check if user is in this room
        is_in_room = user["luogu_name"] in room.members
        room_list.append({
            "id": rid,
            "creator": room.teams.get("team1", [None])[0] or "未知",
            "players": f"Team1: {len(room.teams['team1'])} | Team2: {len(room.teams['team2'])}",
            "status": "进行中" if not room.finished else "已结束",
            "url": url_for("room_page", room_id=rid),
            "is_in_room": is_in_room
        })
    return render_template("index.html", rooms=room_list, current_user=user)

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        luogu_name = request.form.get("luogu_name", "").strip()
        avatar_file = request.files.get("avatar")

        if not luogu_name:
            return "洛谷用户名不能为空", 400

        user_id = str(uuid.uuid4())
        avatar_path = None
        if avatar_file and avatar_file.filename != '':
            # Secure filename and save
            filename = secure_filename(f"{user_id}.png")
            filepath = os.path.join(app.config["AVATAR_FOLDER"], filename)
            avatar_file.save(filepath)
            avatar_path = f"avatars/{filename}"

        users[user_id] = {
            "luogu_name": luogu_name,
            "avatar": avatar_path
        }
        session["user_id"] = user_id
        return redirect(url_for("index"))

    # Check if already logged in
    if get_current_user():
        return redirect(url_for("index"))

    return render_template("register.html")

@app.route("/logout")
def logout():
    session.pop("user_id", None)
    return redirect(url_for("register"))

@app.route("/room/<room_id>")
def room_page(room_id):
    user = get_current_user()
    if not user:
        return redirect(url_for("register"))

    if room_id not in rooms:
        return "房间不存在", 404

    room = rooms[room_id]
    # Check if user is in the room
    if user["luogu_name"] not in room.members:
        return "你不在这个房间中", 403

    return render_template("room.html", room=room.get_status(), current_user=user)


@app.route("/api/leave", methods=["POST"])
def leave_room():
    user = get_current_user()
    if not user:
        return jsonify({"error": "请先注册"}), 401

    data = request.json
    room_id = data.get("room_id")

    if room_id not in rooms:
        return jsonify({"error": "房间不存在"}), 404

    room = rooms[room_id]
    if room.remove_member(user["luogu_name"]):
        # Check if game should end due to empty team (optional rule)
        # if not room.teams["team1"] or not room.teams["team2"]:
        #     room.finished = True
        #     room.winner = "对方队伍全员离线"
        #     socketio.emit("game_over", {"winner": room.winner}, room=room_id)

        # Emit update to all in the room
        socketio.emit("update", room.get_status(), room=room_id)
        return jsonify({"ok": True})
    else:
        return jsonify({"error": "你不在该房间中"}), 400
    
@app.route("/api/accept_proposal", methods=["POST"])
def accept_proposal():
    user = get_current_user()
    if not user:
        return jsonify({"error": "请先注册"}), 401

    data = request.json
    room_id = data.get("room_id")
    pid = data.get("pid")

    if room_id not in rooms:
        return jsonify({"error": "房间不存在"}), 404

    room = rooms[room_id]
    proposal_to_accept = None
    for prop in room.proposals:
        if prop["pid"] == pid and prop["status"] == "pending":
            proposal_to_accept = prop
            break

    if not proposal_to_accept:
        return jsonify({"error": "提案未找到或非待处理状态"}), 404

    proposer_team = proposal_to_accept["proposer"]
    accepter_team = "team1" if proposer_team == "team2" else "team2"
    if user["luogu_name"] not in room.teams.get(accepter_team, []):
        return jsonify({"error": "你不在有权限同意的队伍中"}), 403

    # Accept the proposal
    proposal_to_accept["status"] = "accepted"
    room.problems.add(pid)

    socketio.emit("update", room.get_status(), room=room_id)
    return jsonify({"ok": True})

@app.route("/api/accept_delete", methods=["POST"])
def accept_delete():
    user = get_current_user()
    if not user:
        return jsonify({"error": "请先注册"}), 401

    data = request.json
    room_id = data.get("room_id")
    pid = data.get("pid")

    if room_id not in rooms:
        return jsonify({"error": "房间不存在"}), 404

    room = rooms[room_id]
    proposal_to_accept = None
    for prop in room.deletion_proposals:
        if prop["pid"] == pid and prop["status"] == "pending":
            proposal_to_accept = prop
            break

    if not proposal_to_accept:
        return jsonify({"error": "删除提案未找到或非待处理状态"}), 404

    proposer_team = proposal_to_accept["proposer"]
    accepter_team = "team1" if proposer_team == "team2" else "team2"
    if user["luogu_name"] not in room.teams.get(accepter_team, []):
        return jsonify({"error": "你不在有权限同意的队伍中"}), 403

    proposal_to_accept["status"] = "accepted"
    room.problems.discard(pid)
    room.solved.discard(pid)
    # Also remove from solved_by if it was solved
    if pid in room.solved_by:
        del room.solved_by[pid]
    # Adjust score if necessary (e.g., if a solved problem is deleted, subtract points)
    # For simplicity, we won't subtract points here, as it complicates score tracking.
    # The game state might become inconsistent if scores are adjusted retroactively.

    socketio.emit("update", room.get_status(), room=room_id)
    return jsonify({"ok": True})

# --- SocketIO Events ---
@socketio.on("join_room")
def handle_join_room(data):
    room_id = data["room_id"]
    team = data["team"]
    join_room(room_id)
    join_room(f"{room_id}_{team}")
    emit("message", {"user": "系统", "text": f"欢迎 {team} 队员加入!", "time": time.strftime("%H:%M:%S")}, room=f"{room_id}_{team}")

@socketio.on("chat")
def handle_chat(data):
    room_id = data["room_id"]
    team = data["team"]
    user = data["user"]
    text = data["text"]

    # --- Check for commands ---
    if text.startswith("!propose "):
        pid = text[len("!propose "):].strip()
        if not pid:
            emit("message", {"user": "系统", "text": "格式错误：!propose <题目ID>", "time": time.strftime("%H:%M:%S")}, room=f"{room_id}_{team}")
            return

        room = rooms.get(room_id)
        if not room:
            return

        if user not in room.teams.get(team, []):
             emit("message", {"user": "系统", "text": "你不在该队伍中，无法申请。", "time": time.strftime("%H:%M:%S")}, room=f"{room_id}_{team}")
             return

        # Add proposal to room state
        room.proposals.append({
            "proposer": team,
            "pid": pid,
            "status": "pending",
            "timestamp": time.strftime("%H:%M:%S")
        })
        # Broadcast the proposal request to the entire room
        socketio.emit("proposal_request", {"proposer": team, "pid": pid, "timestamp": time.strftime("%H:%M:%S")}, room=room_id)
        # Also broadcast an update so the proposal list refreshes
        socketio.emit("update", room.get_status(), room=room_id)
        # Send confirmation to the sender's team
        emit("message", {"user": "系统", "text": f"已申请添加题目: {pid}", "time": time.strftime("%H:%M:%S")}, room=f"{room_id}_{team}")
        return # Don't send the command as a normal message

    elif text.startswith("!delete "):
        pid = text[len("!delete "):].strip()
        if not pid:
            emit("message", {"user": "系统", "text": "格式错误：!delete <题目ID>", "time": time.strftime("%H:%M:%S")}, room=f"{room_id}_{team}")
            return

        room = rooms.get(room_id)
        if not room:
            return

        if user not in room.teams.get(team, []):
             emit("message", {"user": "系统", "text": "你不在该队伍中，无法申请。", "time": time.strftime("%H:%M:%S")}, room=f"{room_id}_{team}")
             return

        # Check if problem exists
        if pid not in room.problems:
            emit("message", {"user": "系统", "text": f"题目 {pid} 不存在，无法删除。", "time": time.strftime("%H:%M:%S")}, room=f"{room_id}_{team}")
            return

        # Check if already proposed for deletion
        for prop in room.deletion_proposals:
            if prop["pid"] == pid and prop["status"] == "pending":
                emit("message", {"user": "系统", "text": f"删除申请 {pid} 已存在。", "time": time.strftime("%H:%M:%S")}, room=f"{room_id}_{team}")
                return

        # Add deletion proposal to room state
        room.deletion_proposals.append({
            "proposer": team,
            "pid": pid,
            "status": "pending",
            "timestamp": time.strftime("%H:%M:%S")
        })
        # Broadcast the deletion proposal request to the entire room
        socketio.emit("deletion_request", {"proposer": team, "pid": pid, "timestamp": time.strftime("%H:%M:%S")}, room=room_id)
        # Also broadcast an update so the deletion proposal list refreshes
        socketio.emit("update", room.get_status(), room=room_id)
        # Send confirmation to the sender's team
        emit("message", {"user": "系统", "text": f"已申请删除题目: {pid} (需对方同意)", "time": time.strftime("%H:%M:%S")}, room=f"{room_id}_{team}")
        return # Don't send the command as a normal message

    # --- Send normal message ---
    emit("message", {"user": user, "text": text, "time": time.strftime("%H:%M:%S")}, room=f"{room_id}_{team}")

@app.route("/api/propose_delete", methods=["POST"])
def propose_delete():
    user = get_current_user()
    if not user:
        return jsonify({"error": "请先注册"}), 401

    data = request.json
    room_id = data.get("room_id")
    pid = data.get("pid")
    proposer_team = data.get("team")

    if room_id not in rooms:
        return jsonify({"error": "房间不存在"}), 404

    room = rooms[room_id]
    # Check if user is in the proposing team
    if user["luogu_name"] not in room.teams.get(proposer_team, []):
         return jsonify({"error": "你不在该队伍中"}), 403

    # Check if problem exists
    if pid not in room.problems:
        return jsonify({"error": "题目不存在"}), 400

    # Check if already proposed
    for prop in room.deletion_proposals:
        if prop["pid"] == pid and prop["status"] == "pending":
            return jsonify({"error": "删除申请已存在"}), 400

    room.deletion_proposals.append({
        "proposer": proposer_team,
        "pid": pid,
        "status": "pending",
        "timestamp": time.strftime("%H:%M:%S")
    })
    # Emit deletion proposal notification to the room
    socketio.emit("deletion_proposal", {"proposer": proposer_team, "pid": pid}, room=room_id)
    return jsonify({"ok": True})

@app.route("/api/create", methods=["POST"])
def create_room():
    user = get_current_user()
    if not user:
        return jsonify({"error": "请先注册"}), 401

    data = request.json
    custom_problems = data.get("problems", ["P1000"])

    room_id = str(uuid.uuid4())[:8]
    room = Room(room_id)
    room.problems = set(custom_problems)
    room.add_member("team1", user["luogu_name"])

    rooms[room_id] = room
    threading.Thread(target=judge_room, args=(room_id,), daemon=True).start()
    return jsonify({"room_id": room_id, "url": url_for("room_page", room_id=room_id, _external=True)})

@app.route("/api/join", methods=["POST"])
def join_room_api():
    user = get_current_user()
    if not user:
        return jsonify({"error": "请先注册"}), 401

    data = request.json
    room_id = data.get("room_id")
    team = data.get("team")

    if not room_id or team not in ["team1", "team2"]:
        return jsonify({"error": "房间或队伍无效"}), 400

    if room_id not in rooms:
        return jsonify({"error": "房间不存在"}), 404

    room = rooms[room_id]
    if user["luogu_name"] in room.members:
        return jsonify({"error": "你已在此房间中"}), 400

    if room.add_member(team, user["luogu_name"]):
        socketio.emit("update", room.get_status(), room=room_id)
        return jsonify({"ok": True})
    else:
        return jsonify({"error": "无法加入队伍"}), 400

@app.route("/api/propose", methods=["POST"])
def propose_problem():
    user = get_current_user()
    if not user:
        return jsonify({"error": "请先注册"}), 401

    data = request.json
    room_id = data.get("room_id")
    pid = data.get("pid")
    proposer_team = data.get("team")

    if room_id not in rooms:
        return jsonify({"error": "房间不存在"}), 404

    room = rooms[room_id]
    if user["luogu_name"] not in room.teams.get(proposer_team, []):
         return jsonify({"error": "你不在该队伍中"}), 403

    room.proposals.append({
        "proposer": proposer_team,
        "pid": pid,
        "status": "pending",
        "timestamp": time.strftime("%H:%M:%S")
    })
    socketio.emit("proposal", {"proposer": proposer_team, "pid": pid}, room=room_id)
    return jsonify({"ok": True})



# ----------------------------
# Static File Serving for Avatars
# ----------------------------
@app.route('/static/avatars/<filename>')
def uploaded_avatar(filename):
    return send_from_directory(app.config['AVATAR_FOLDER'], filename)

@app.route("/api/reject_proposal", methods=["POST"])
def reject_proposal():
    user = get_current_user()
    if not user:
        return jsonify({"error": "请先注册"}), 401

    data = request.json
    room_id = data.get("room_id")
    pid = data.get("pid")

    if room_id not in rooms:
        return jsonify({"error": "房间不存在"}), 404

    room = rooms[room_id]
    proposal_to_reject = None
    for prop in room.proposals:
        if prop["pid"] == pid and prop["status"] == "pending":
            proposal_to_reject = prop
            break

    if not proposal_to_reject:
        return jsonify({"error": "提案未找到或非待处理状态"}), 404

    proposer_team = proposal_to_reject["proposer"]
    rejecter_team = "team1" if proposer_team == "team2" else "team2"
    if user["luogu_name"] not in room.teams.get(rejecter_team, []):
        return jsonify({"error": "你不在有权限拒绝的队伍中"}), 403

    # Reject the proposal
    proposal_to_reject["status"] = "rejected"

    socketio.emit("update", room.get_status(), room=room_id)
    return jsonify({"ok": True})

@app.route("/api/reject_delete", methods=["POST"])
def reject_delete():
    user = get_current_user()
    if not user:
        return jsonify({"error": "请先注册"}), 401

    data = request.json
    room_id = data.get("room_id")
    pid = data.get("pid")

    if room_id not in rooms:
        return jsonify({"error": "房间不存在"}), 404

    room = rooms[room_id]
    proposal_to_reject = None
    for prop in room.deletion_proposals:
        if prop["pid"] == pid and prop["status"] == "pending":
            proposal_to_reject = prop
            break

    if not proposal_to_reject:
        return jsonify({"error": "删除提案未找到或非待处理状态"}), 404

    proposer_team = proposal_to_reject["proposer"]
    rejecter_team = "team1" if proposer_team == "team2" else "team2"
    if user["luogu_name"] not in room.teams.get(rejecter_team, []):
        return jsonify({"error": "你不在有权限拒绝的队伍中"}), 403

    proposal_to_reject["status"] = "rejected"

    socketio.emit("update", room.get_status(), room=room_id)
    return jsonify({"ok": True})

# ----------------------------
# Main
# ----------------------------
if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000, debug=True)
