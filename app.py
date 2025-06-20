from fastapi import FastAPI, Form, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from telethon import TelegramClient
import os

api_id = 21917577
api_hash = '87b990474f921f9fee7a32631cad3427'
phone_number = '+380502022836'

app = FastAPI()
templates = Jinja2Templates(directory="templates")
client = TelegramClient('web_session', api_id, api_hash)
started = False

@app.on_event("startup")
async def startup_event():
    global started
    if not started:
        await client.connect()
        if not await client.is_user_authorized():
            await client.send_code_request(phone_number)
            code = input("Enter the login code sent to your Telegram: ")
            await client.sign_in(phone_number, code)
        started = True

async def get_dialogs():
    dialogs = []
    async for dialog in client.iter_dialogs():
        dialogs.append((dialog.id, dialog.name))
    return dialogs

async def get_last_messages(chat_id: int, limit: int = 10):
    messages = []
    async for msg in client.iter_messages(chat_id, limit=limit):
        if msg.text:
            messages.append({"type": "text", "content": f"{msg.sender_id}: {msg.text}"})
        elif msg.media:
            filename = f"{msg.id}_{msg.file.name or 'file'}"
            filepath = os.path.join(MEDIA_FOLDER, filename)

            # Download file only if not already saved
            if not os.path.exists(filepath):
                await msg.download_media(file=filepath)

            ext = os.path.splitext(filename)[1].lower()
            if ext in [".jpg", ".jpeg", ".png", ".gif"]:
                media_type = "image"
            elif ext in [".mp4", ".mov", ".webm"]:
                media_type = "video"
            elif ext in [".mp3", ".wav", ".ogg"]:
                media_type = "audio"
            else:
                media_type = "file"

            messages.append({
                "type": media_type,
                "filename": filename,
                "sender": msg.sender_id
            })
    return messages[::-1]



@app.get("/", response_class=HTMLResponse)
async def form(request: Request):
    dialogs = await get_dialogs()
    return templates.TemplateResponse("index.html", {"request": request, "dialogs": dialogs, "messages": []})

@app.post("/", response_class=HTMLResponse)
async def send(
    request: Request,
    chat_id: int = Form(...),
    message: str = Form(None),
    read: str = Form(None)
):
    dialogs = await get_dialogs()
    messages = []

    if message:
        await client.send_message(chat_id, message)

    if read:
        messages = await get_last_messages(chat_id)

    return templates.TemplateResponse("index.html", {
        "request": request,
        "dialogs": dialogs,
        "messages": messages,
        "active_chat": chat_id,
        "success": bool(message)
    })

@app.post("/upload")
async def upload_file(chat_id: int = Form(...), file: UploadFile = File(...)):
    filename = file.filename
    file_path = os.path.join(MEDIA_FOLDER, filename)

    with open(file_path, "wb") as f:
        content = await file.read()
        f.write(content)

    await client.send_file(chat_id, file_path)
    return JSONResponse(content={"success": True})


@app.get("/messages/{chat_id}")
async def get_messages(chat_id: int):
    messages = await get_last_messages(chat_id)
    return {"messages": messages}


from fastapi.responses import FileResponse

MEDIA_FOLDER = "media"

@app.on_event("startup")
async def ensure_media_folder():
    os.makedirs(MEDIA_FOLDER, exist_ok=True)

from fastapi.responses import FileResponse
import mimetypes

@app.get("/media/{filename}")
async def get_media(filename: str):
    path = os.path.join(MEDIA_FOLDER, filename)
    if os.path.exists(path):
        mime_type, _ = mimetypes.guess_type(path)
        return FileResponse(path, media_type=mime_type or 'application/octet-stream')
    return JSONResponse(content={"error": "File not found"}, status_code=404)

from fastapi import responses

@app.get("/files", response_class=HTMLResponse)
async def list_files(request: Request):
    files = []
    for filename in os.listdir(MEDIA_FOLDER):
        path = os.path.join(MEDIA_FOLDER, filename)
        if os.path.isfile(path):
            ext = os.path.splitext(filename)[1].lower()
            if ext in [".jpg", ".jpeg", ".png", ".gif"]:
                media_type = "image"
            elif ext in [".mp4", ".mov", ".webm"]:
                media_type = "video"
            elif ext in [".mp3", ".wav", ".ogg"]:
                media_type = "audio"
            else:
                media_type = "file"
            files.append({"name": filename, "type": media_type})
    return templates.TemplateResponse("files.html", {"request": request, "files": files})

