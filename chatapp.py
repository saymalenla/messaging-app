import asyncio
import json
import os
import base64
import websockets
from fastapi import FastAPI, WebSocket, HTTPException
from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials
import ttkbootstrap as ttk
from ttkbootstrap.constants import *
from ttkbootstrap.style import Style
from PIL import Image, ImageTk
import sounddevice as sd
import numpy as np
import scipy.io.wavfile as wavfile
import threading
import requests
import uuid
import tempfile
from datetime import datetime

# === Server Code ===
app = FastAPI()

# Store connected clients and authenticated users
clients = {}
user_count = 0

# Helper function to create Google credentials from environment variable
def get_google_credentials():
    credentials_json = os.getenv("GOOGLE_CREDENTIALS")
    if not credentials_json:
        raise HTTPException(status_code=500, detail="Google credentials not configured")
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        f.write(credentials_json)
        return f.name

# Helper function to validate Google token
def validate_google_token(token):
    try:
        response = requests.get(
            f"https://www.googleapis.com/oauth2/v3/tokeninfo?access_token={token}"
        )
        if response.status_code == 200:
            return response.json()
        return None
    except:
        return None

@app.websocket("/ws/{client_id}")
async def websocket_endpoint(websocket: WebSocket, client_id: str):
    global user_count
    await websocket.accept()
    
    # First client doesn't need Google login
    if user_count == 0:
        clients[client_id] = {"websocket": websocket, "email": "first_user"}
        user_count += 1
        await websocket.send_json({"type": "auth", "status": "success", "email": "first_user"})
    else:
        # Require Google login for subsequent clients
        await websocket.send_json({"type": "auth", "status": "login_required"})
        try:
            data = await websocket.receive_json()
            if data["type"] == "google_token":
                token = data["token"]
                user_info = validate_google_token(token)
                if user_info and "email" in user_info:
                    clients[client_id] = {"websocket": websocket, "email": user_info["email"]}
                    user_count += 1
                    await websocket.send_json({"type": "auth", "status": "success", "email": user_info["email"]})
                else:
                    await websocket.send_json({"type": "auth", "status": "failed"})
                    await websocket.close()
                    return
        except:
            await websocket.close()
            return

    try:
        while True:
            data = await websocket.receive_json()
            if data["type"] == "message":
                # Broadcast text message
                for cid, client in clients.items():
                    if cid != client_id:
                        await client["websocket"].send_json({
                            "type": "message",
                            "sender": clients[client_id]["email"],
                            "content": data["content"],
                            "timestamp": datetime.utcnow().isoformat()
                        })
            elif data["type"] == "file":
                # Broadcast file (voice/sticker)
                for cid, client in clients.items():
                    if cid != client_id:
                        await client["websocket"].send_json({
                            "type": "file",
                            "sender": clients[client_id]["email"],
                            "filename": data["filename"],
                            "content": data["content"],
                            "timestamp": datetime.utcnow().isoformat()
                        })
    except:
        del clients[client_id]
        user_count -= 1
        await websocket.close()

# === Client Code ===
class MessagingApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Ứng dụng Nhắn Tin")
        self.client_id = str(uuid.uuid4())
        self.websocket = None
        self.email = None
        self.recording = False
        self.audio_data = []
        self.images = []  # Store image references to prevent garbage collection

        # Initialize ttkbootstrap style
        self.style = Style()
        self.themes = {
            "Tình Yêu": "litera",  # Pinkish, light theme
            "Phong Cảnh": "flatly",  # Greenish, nature-like
            "Dark Mode": "superhero",  # Dark, modern
            "Cyberpunk": "vapor",  # Neon, futuristic
            "Pastel": "minty"  # Soft colors
        }

        # GUI Setup
        self.main_frame = ttk.Frame(self.root, padding=10)
        self.main_frame.pack(fill=BOTH, expand=True)

        # Theme selector
        self.theme_frame = ttk.Frame(self.main_frame)
        self.theme_frame.pack(fill=X, pady=5)
        ttk.Label(self.theme_frame, text="Chọn Theme:").pack(side=LEFT)
        self.theme_var = ttk.StringVar(value="Tình Yêu")
        self.theme_menu = ttk.OptionMenu(
            self.theme_frame, self.theme_var, "Tình Yêu", *self.themes.keys(),
            command=self.change_theme
        )
        self.theme_menu.pack(side=LEFT, padx=5)

        # Chat display
        self.chat_frame = ttk.Frame(self.main_frame)
        self.chat_frame.pack(fill=BOTH, expand=True)
        self.chat_text = ttk.Text(self.chat_frame, height=20, state="disabled", wrap=WORD)
        self.chat_text.pack(fill=BOTH, expand=True, padx=5, pady=5)
        scrollbar = ttk.Scrollbar(self.chat_frame, orient=VERTICAL, command=self.chat_text.yview)
        scrollbar.pack(side=RIGHT, fill=Y)
        self.chat_text.config(yscrollcommand=scrollbar.set)

        # Input area
        self.input_frame = ttk.Frame(self.main_frame)
        self.input_frame.pack(fill=X, pady=5)
        self.message_entry = ttk.Entry(self.input_frame)
        self.message_entry.pack(side=LEFT, fill=X, expand=True, padx=5)
        self.send_button = ttk.Button(self.input_frame, text="Gửi", style="primary.TButton", command=self.send_message)
        self.send_button.pack(side=RIGHT)

        # Media controls
        self.media_frame = ttk.Frame(self.main_frame)
        self.media_frame.pack(fill=X, pady=5)
        self.record_button = ttk.Button(self.media_frame, text="Ghi Âm", style="info.TButton", command=self.toggle_recording)
        self.record_button.pack(side=LEFT, padx=5)
        self.sticker_button = ttk.Button(self.media_frame, text="Gửi Sticker", style="success.TButton", command=self.send_sticker)
        self.sticker_button.pack(side=LEFT, padx=5)

        # Connect to server
        self.connect_to_server()

    def change_theme(self, *args):
        theme_name = self.themes[self.theme_var.get()]
        self.style.theme_use(theme_name)

    def connect_to_server(self):
        threading.Thread(target=self.run_websocket, daemon=True).start()

    def run_websocket(self):
        asyncio.run(self.websocket_client())

    async def websocket_client(self):
        # Replace with your Render URL after deployment
        uri = "wss://your-app-name.onrender.com/ws/" + self.client_id
        try:
            async with websockets.connect(uri) as websocket:
                self.websocket = websocket
                while True:
                    try:
                        message = await websocket.recv()
                        data = json.loads(message)
                        if data["type"] == "auth":
                            if data["status"] == "success":
                                self.email = data["email"]
                                self.display_message(f"Đã kết nối với {self.email}")
                            elif data["status"] == "login_required":
                                self.google_login()
                        elif data["type"] == "message":
                            self.display_message(f"{data['sender']}: {data['content']}", data["timestamp"])
                        elif data["type"] == "file":
                            self.handle_file(data)
                    except websockets.exceptions.ConnectionClosed:
                        self.display_message("Mất kết nối với server")
                        break
        except Exception as e:
            self.display_message(f"Lỗi kết nối: {str(e)}")

    def google_login(self):
        try:
            credentials_file = get_google_credentials()
            flow = InstalledAppFlow.from_client_secrets_file(
                credentials_file,
                scopes=["https://www.googleapis.com/auth/userinfo.email"]
            )
            creds = flow.run_local_server(port=0)
            token = creds.token
            os.remove(credentials_file)
            threading.Thread(target=self.send_google_token, args=(token,), daemon=True).start()
        except Exception as e:
            self.display_message(f"Lỗi đăng nhập Google: {str(e)}")

    def send_google_token(self, token):
        asyncio.run(self.send_token(token))

    async def send_token(self, token):
        await self.websocket.send_json({"type": "google_token", "token": token})

    def send_message(self):
        content = self.message_entry.get().strip()
        if content and self.websocket:
            threading.Thread(target=self.send_text_message, args=(content,), daemon=True).start()
            self.message_entry.delete(0, END)

    def send_text_message(self, content):
        asyncio.run(self.send_text(content))

    async def send_text(self, content):
        await self.websocket.send_json({"type": "message", "content": content})
        self.display_message(f"Bạn: {content}", datetime.utcnow().isoformat())

    def toggle_recording(self):
        if not self.recording:
            self.recording = True
            self.record_button.config(text="Dừng Ghi Âm")
            self.audio_data = []
            threading.Thread(target=self.record_audio, daemon=True).start()
        else:
            self.recording = False
            self.record_button.config(text="Ghi Âm")
            self.save_and_send_audio()

    def record_audio(self):
        fs = 44100
        while self.recording:
            data = sd.rec(int(0.5 * fs), samplerate=fs, channels=1)
            sd.wait()
            self.audio_data.append(data)
        sd.stop()

    def save_and_send_audio(self):
        fs = 44100
        audio = np.concatenate(self.audio_data, axis=0)
        filename = f"voice_{uuid.uuid4()}.wav"
        wavfile.write(filename, fs, audio)
        with open(filename, "rb") as f:
            content = base64.b64encode(f.read()).decode()
        threading.Thread(target=self.send_file, args=(filename, content), daemon=True).start()
        os.remove(filename)

    def send_sticker(self):
        filename = ttk.filedialog.askopenfilename(filetypes=[("Image files", "*.png *.jpg *.jpeg")])
        if filename:
            with open(filename, "rb") as f:
                content = base64.b64encode(f.read()).decode()
            threading.Thread(target=self.send_file, args=(filename, content), daemon=True).start()

    def send_file(self, filename, content):
        asyncio.run(self.send_file_content(filename, content))

    async def send_file_content(self, filename, content):
        await self.websocket.send_json({
            "type": "file",
            "filename": os.path.basename(filename),
            "content": content
        })
        self.display_message(f"Bạn đã gửi: {os.path.basename(filename)}", datetime.utcnow().isoformat())

    def handle_file(self, data):
        filename = data["filename"]
        content = base64.b64decode(data["content"])
        with open(filename, "wb") as f:
            f.write(content)
        if filename.endswith(".wav"):
            self.display_message(f"{data['sender']} gửi tin nhắn thoại: {filename}", data["timestamp"])
        elif filename.endswith((".png", ".jpg", ".jpeg")):
            self.display_image(filename, data["sender"], data["timestamp"])
        os.remove(filename)

    def display_message(self, message, timestamp):
        self.chat_text.config(state="normal")
        formatted_time = datetime.fromisoformat(timestamp).strftime("%H:%M:%S")
        self.chat_text.insert(END, f"[{formatted_time}] {message}\n")
        self.chat_text.config(state="disabled")
        self.chat_text.see(END)

    def display_image(self, filename, sender, timestamp):
        img = Image.open(filename)
        img = img.resize((100, 100), Image.Resampling.LANCZOS)
        photo = ImageTk.PhotoImage(img)
        self.images.append(photo)  # Prevent garbage collection
        self.chat_text.config(state="normal")
        formatted_time = datetime.fromisoformat(timestamp).strftime("%H:%M:%S")
        self.chat_text.insert(END, f"[{formatted_time}] {sender} gửi sticker:\n")
        self.chat_text.image_create(END, image=photo)
        self.chat_text.insert(END, "\n")
        self.chat_text.config(state="disabled")
        self.chat_text.see(END)

# Run Server and Client
if __name__ == "__main__":
    import uvicorn
    threading.Thread(
        target=lambda: uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000))),
        daemon=True
    ).start()
    root = ttk.Window(themename="litera")
    app = MessagingApp(root)
    root.geometry("600x500")
    root.mainloop()