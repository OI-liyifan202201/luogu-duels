const socket = io();
const roomId = window.location.pathname.split("/")[2]; // 从 URL 获取 room_id
let myTeam = localStorage.getItem("my_team") || "team1";

// 请求通知权限
if ("Notification" in window) {
    Notification.requestPermission();
}

// 初始化连接
socket.emit("join_room", {room_id: roomId, team: myTeam});

socket.on("update", (data) => {
    document.getElementById("score1").innerText = data.scores.team1;
    document.getElementById("score2").innerText = data.scores.team2;
    document.getElementById("solved").innerText = data.solved.join(", ");
    if (data.finished) {
        document.getElementById("winner-animation").innerText = `🏆 ${data.winner} 获胜！`;
        document.getElementById("winner-animation").style.display = "block";
    }
});

socket.on("game_over", (data) => {
    document.getElementById("winner-animation").innerText = `🏆 ${data.winner} 获胜！`;
    document.getElementById("winner-animation").style.display = "block";
});

socket.on("message", (msg) => {
    const messages = document.getElementById("messages");
    messages.innerHTML += `<p><b>${msg.user}</b> (${msg.time}): ${msg.text}</p>`;
    messages.scrollTop = messages.scrollHeight;

    // 显示通知
    if (Notification.permission === "granted") {
        new Notification("新消息", {
            body: `${msg.user}: ${msg.text}`,
            icon: "/static/logo.png"
        });
    }
});

function sendChat() {
    const user = document.getElementById("user-input").value;
    const text = document.getElementById("msg-input").value;
    if (user && text) {
        socket.emit("chat", {room_id: roomId, team: myTeam, user, text});
        document.getElementById("msg-input").value = "";
    }
}

function proposeProblem() {
    const pid = document.getElementById("new-pid").value;
    if (pid) {
        fetch("/api/propose", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({room_id: roomId, pid: pid, team: myTeam})
        });
    }
}