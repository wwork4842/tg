import os
import base64
from io import BytesIO
from fastapi import FastAPI, Request, HTTPException, Form, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from telethon import TelegramClient
from telethon.tl.types import Chat, Channel, DocumentAttributeVideo, DocumentAttributeFilename
from telethon.events import NewMessage
from telethon.errors import SessionPasswordNeededError, ChatAdminRequiredError
from telethon.tl.functions.channels import GetParticipantsRequest
from telethon.tl.types import ChannelParticipantsSearch
from dotenv import load_dotenv
from datetime import datetime
import mimetypes

# Load credentials from .env
load_dotenv()
API_ID = int(os.getenv("TG_API_ID"))
API_HASH = os.getenv("TG_API_HASH")
PHONE_NUMBER = os.getenv("TG_PHONE")

client = TelegramClient("web_session", API_ID, API_HASH)
app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key="your-secret-key")
templates = Jinja2Templates(directory="templates")
started = False


# Startup
@app.on_event("startup")
async def startup_event():
    global started
    try:
        if not started:
            await client.connect()
            if not await client.is_user_authorized():
                await client.send_code_request(PHONE_NUMBER)
                raise HTTPException(status_code=307, detail="Redirect to /authorize")
            started = True
    except Exception as e:
        print(f"Startup error: {e}")
        raise HTTPException(status_code=500, detail="Failed to initialize Telegram client")


# Authorization endpoint
@app.post("/authorize")
async def authorize(code: str = Form(...)):
    try:
        await client.sign_in(PHONE_NUMBER, code)
        return RedirectResponse(url="/", status_code=303)
    except SessionPasswordNeededError:
        raise HTTPException(status_code=400, detail="2FA password required")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Authorization failed: {e}")


@app.get("/authorize", response_class=HTMLResponse)
async def authorize_form(request: Request):
    return templates.TemplateResponse("authorize.html", {"request": request})


# Helper: List group chats (including supergroups and channels)
async def get_group_chats():
    try:
        groups = []
        async for dialog in client.iter_dialogs():
            entity = dialog.entity
            if isinstance(entity, (Channel, Chat)) and not entity.creator and not entity.left:
                name = f"{dialog.name} (@{entity.username})" if entity.username else dialog.name
                groups.append(
                    {"id": dialog.id, "name": name, "type": "channel" if isinstance(entity, Channel) else "group"})
        return groups
    except Exception as e:
        print(f"Error fetching groups: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch group chats")


# Helper: List last messages from group chats with filters and detailed user info
async def get_last_messages(chat_id: int, limit: int = 100, offset: int = 0, query: str = None, start_date: str = None,
                            end_date: str = None):
    try:
        messages = []
        async for msg in client.iter_messages(chat_id, limit=limit, offset_id=offset):
            if msg.text or msg.media:
                if query and query.lower() not in (msg.text or "").lower():
                    continue
                if start_date or end_date:
                    msg_date = msg.date.date()
                    start = datetime.strptime(start_date, "%Y-%m-%d").date() if start_date else None
                    end = datetime.strptime(end_date, "%Y-%m-%d").date() if end_date else None
                    if start and msg_date < start:
                        continue
                    if end and msg_date > end:
                        continue
                sender = await msg.get_sender()
                user_info = {
                    "id": sender.id if sender else "Unknown",
                    "first_name": getattr(sender, "first_name", "Unknown"),
                    "last_name": getattr(sender, "last_name", ""),
                    "username": getattr(sender, "username", "No username"),
                    "phone": getattr(sender, "phone", "Hidden")
                }
                content = msg.text if msg.text else f"[Media: {msg.media.__class__.__name__}]"
                messages.append({"content": content, "date": msg.date, "id": msg.id, "user": user_info})
        return messages
    except Exception as e:
        print(f"Error fetching messages: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch messages")


# Helper: List all users in a chat with proper pagination
async def get_chat_users(chat_id: int, limit: int = 100, offset: int = 0):
    try:
        users = []
        entity = await client.get_entity(chat_id)
        if isinstance(entity, Channel):
            participants = await client(GetParticipantsRequest(
                channel=entity,
                filter=ChannelParticipantsSearch(''),
                offset=offset,
                limit=limit,
                hash=0
            ))
            for user in participants.users:
                user_info = {
                    "id": user.id,
                    "first_name": getattr(user, "first_name", "Unknown"),
                    "last_name": getattr(user, "last_name", ""),
                    "username": getattr(user, "username", "No username"),
                    "phone": getattr(user, "phone", "Hidden")
                }
                users.append(user_info)
        else:
            async for user in client.iter_participants(chat_id, limit=limit):
                if offset > 0:
                    offset -= 1
                    continue
                user_info = {
                    "id": user.id,
                    "first_name": getattr(user, "first_name", "Unknown"),
                    "last_name": getattr(user, "last_name", ""),
                    "username": getattr(user, "username", "No username"),
                    "phone": getattr(user, "phone", "Hidden")
                }
                users.append(user_info)
        return {"users": users, "error": None}
    except ChatAdminRequiredError:
        return {"users": [], "error": "Access to the user list is restricted. Administrative rights are required."}
    except Exception as e:
        print(f"Error fetching users: {e}")
        return {"users": [], "error": f"Failed to fetch users: {str(e)}"}


# Helper: List all media files in a chat with full content
async def get_chat_media(chat_id: int, limit: int = 20, offset: int = 0):
    try:
        media_files = []
        async for msg in client.iter_messages(chat_id, limit=limit, offset_id=offset, filter=None):
            if msg.media:
                sender = await msg.get_sender()
                user_info = {
                    "id": sender.id if sender else "Unknown",
                    "first_name": getattr(sender, "first_name", "Unknown"),
                    "last_name": getattr(sender, "last_name", ""),
                    "username": getattr(sender, "username", "No username"),
                    "phone": getattr(sender, "phone", "Hidden")
                }
                media_type = msg.media.__class__.__name__
                media_data = {}

                # Завантажуємо медіа
                try:
                    media_bytes = await client.download_media(msg.media, file=BytesIO())
                    if media_type == "Photo":
                        media_data = {
                            "type": "image",
                            "base64": f"data:image/jpeg;base64,{base64.b64encode(media_bytes.getvalue()).decode('utf-8')}",
                            "filename": None,
                            "mime_type": "image/jpeg"
                        }
                    elif media_type == "Document":
                        # Перевіряємо, чи це відео
                        is_video = any(
                            isinstance(attr, DocumentAttributeVideo) for attr in msg.media.document.attributes)
                        filename = next((attr.file_name for attr in msg.media.document.attributes if
                                         isinstance(attr, DocumentAttributeFilename)), "document")
                        mime_type, _ = mimetypes.guess_type(filename)
                        if is_video:
                            media_data = {
                                "type": "video",
                                "base64": f"data:video/mp4;base64,{base64.b64encode(media_bytes.getvalue()).decode('utf-8')}",
                                "filename": None,
                                "mime_type": "video/mp4"
                            }
                        else:
                            media_data = {
                                "type": "document",
                                "base64": f"data:{mime_type or 'application/octet-stream'};base64,{base64.b64encode(media_bytes.getvalue()).decode('utf-8')}",
                                "filename": filename,
                                "mime_type": mime_type or "application/octet-stream"
                            }
                    else:
                        media_data = {
                            "type": "unsupported",
                            "base64": None,
                            "filename": None,
                            "mime_type": None
                        }
                except Exception as e:
                    print(f"Error downloading media {msg.id}: {e}")
                    media_data = {
                        "type": "error",
                        "base64": None,
                        "filename": None,
                        "mime_type": None
                    }

                media_files.append({
                    "id": msg.id,
                    "type": media_type,
                    "media_data": media_data,
                    "date": msg.date,
                    "user": user_info
                })
        return media_files
    except Exception as e:
        print(f"Error fetching media: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch media files")


# Incoming message handler (logging group messages with user info)
@client.on(NewMessage(incoming=True))
async def handle_message(event):
    if event.is_group or event.is_channel:
        sender = await event.get_sender()
        text = event.raw_text
        if text.strip():
            user_info = {
                "id": sender.id,
                "first_name": getattr(sender, "first_name", "Unknown"),
                "last_name": getattr(sender, "last_name", ""),
                "username": getattr(sender, "username", "No username"),
                "phone": getattr(sender, "phone", "Hidden")
            }
            print(f"Group Message from {user_info} in {event.chat_id}: {text}")


# Reset chat selection
@app.get("/reset-chat", response_class=RedirectResponse)
async def reset_chat(request: Request):
    request.session.pop("chat_id", None)
    return RedirectResponse(url="/", status_code=303)


# Main chat UI with search and date filters for group chats
@app.get("/", response_class=HTMLResponse)
async def form(
        request: Request,
        chat_id: int = None,
        query: str = None,
        start_date: str = None,
        end_date: str = None,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=200),
        tab: str = Query(default="messages")
):
    # Зберігаємо chat_id у сесії, якщо він переданий
    if chat_id is not None:
        request.session["chat_id"] = chat_id
    # Якщо chat_id не переданий, беремо з сесії
    elif "chat_id" in request.session:
        chat_id = request.session["chat_id"]

    groups = await get_group_chats()
    # Перевіряємо, чи збережений chat_id є валідним
    if chat_id is not None:
        valid_chat_ids = [group["id"] for group in groups]
        if chat_id not in valid_chat_ids:
            request.session.pop("chat_id", None)
            chat_id = None

    messages = []
    users_data = {"users": [], "error": None}
    media_files = []
    if chat_id:
        if tab == "messages":
            messages = await get_last_messages(chat_id, limit=limit, offset=offset, query=query, start_date=start_date,
                                               end_date=end_date)
        elif tab == "users":
            users_data = await get_chat_users(chat_id, limit=limit, offset=offset)
        elif tab == "media":
            media_files = await get_chat_media(chat_id, limit=20, offset=offset)
    return templates.TemplateResponse("index.html", {
        "request": request,
        "groups": groups,
        "messages": messages,
        "users": users_data["users"],
        "users_error": users_data["error"],
        "media_files": media_files,
        "active_chat": chat_id,
        "query": query,
        "start_date": start_date,
        "end_date": end_date,
        "offset": offset,
        "tab": tab
    })