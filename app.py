import os
import base64
from io import BytesIO
from fastapi import FastAPI, Request, HTTPException, Form, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from telethon import TelegramClient
from telethon.tl.types import Chat, Channel, DocumentAttributeVideo, DocumentAttributeFilename, MessageMediaPhoto, \
    DocumentAttributeAudio, MessageMediaDocument
from telethon.events import NewMessage
from telethon.errors import SessionPasswordNeededError, ChatAdminRequiredError
from telethon.tl.functions.channels import GetParticipantsRequest
from telethon.tl.types import ChannelParticipantsSearch
from dotenv import load_dotenv
from datetime import datetime
import mimetypes
import asyncio
import logging
import hashlib
import glob
import time

# Configure logging to file
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("app.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

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

# Cache for group chats and profile photos
_groups_cache = None
_profile_photo_cache = {}
_media_cache_dir = "media_cache"
os.makedirs(_media_cache_dir, exist_ok=True)


async def get_group_chats():
    global _groups_cache
    if _groups_cache is not None:
        logger.info("Returning cached group chats")
        return _groups_cache
    try:
        logger.info("Fetching group chats from Telegram API")
        groups = []
        async for dialog in client.iter_dialogs():
            entity = dialog.entity
            if isinstance(entity, (Channel, Chat)) and not entity.creator and not entity.left:
                name = f"{dialog.name} (@{entity.username})" if entity.username else dialog.name
                groups.append(
                    {"id": dialog.id, "name": name, "type": "channel" if isinstance(entity, Channel) else "group"})
        _groups_cache = groups
        logger.info(f"Cached {len(groups)} group chats")
        return groups
    except Exception as e:
        logger.error(f"Error fetching groups: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch group chats")


# Startup
@app.on_event("startup")
async def startup_event():
    global started
    try:
        if not started:
            logger.info("Connecting to Telegram")
            await client.connect()
            if not await client.is_user_authorized():
                logger.info("User not authorized, requesting code")
                await client.send_code_request(PHONE_NUMBER)
                raise HTTPException(status_code=307, detail="Redirect to /authorize")
            clean_media_cache()  # Clean media cache on startup
            started = True
            logger.info("Telegram client initialized")
    except Exception as e:
        logger.error(f"Startup error: {e}")
        raise HTTPException(status_code=500, detail="Failed to initialize Telegram client")


# Authorization endpoint
@app.post("/authorize")
async def authorize(code: str = Form(...)):
    try:
        logger.info("Attempting to sign in with code")
        await client.sign_in(PHONE_NUMBER, code)
        logger.info("Authorization successful")
        return RedirectResponse(url="/", status_code=303)
    except SessionPasswordNeededError:
        logger.error("2FA password required")
        raise HTTPException(status_code=400, detail="2FA password required")
    except Exception as e:
        logger.error(f"Authorization failed: {e}")
        raise HTTPException(status_code=400, detail=f"Authorization failed: {e}")


@app.get("/authorize", response_class=HTMLResponse)
async def authorize_form(request: Request):
    logger.info("Rendering authorization form")
    return templates.TemplateResponse("authorize.html", {"request": request})


# Helper: Clean media cache
def clean_media_cache():
    logger.info("Cleaning media cache")
    for f in glob.glob(f"{_media_cache_dir}/*.bin"):
        if os.path.getmtime(f) < time.time() - 7 * 86400:
            os.remove(f)
            logger.debug(f"Removed old cache file {f}")


# Helper: Download profile photo with caching
async def download_profile_photo(user_id: int, semaphore: asyncio.Semaphore):
    global _profile_photo_cache
    async with semaphore:
        cache_key = f"user_{user_id}"
        if cache_key in _profile_photo_cache:
            logger.debug(f"Returning cached profile photo for user {user_id}")
            return _profile_photo_cache[cache_key]
        try:
            await asyncio.sleep(0.1)  # Rate limiting
            photo_file = await client.download_profile_photo(user_id, file=BytesIO())
            if photo_file:
                photo_data = f"data:image/jpeg;base64,{base64.b64encode(photo_file.getvalue()).decode('utf-8')}"
                _profile_photo_cache[cache_key] = photo_data
                logger.debug(f"Cached profile photo for user {user_id}")
            else:
                _profile_photo_cache[cache_key] = None
                logger.debug(f"No profile photo for user {user_id}")
            # Clean cache if too large
            if len(_profile_photo_cache) > 1000:
                logger.info("Clearing profile photo cache")
                _profile_photo_cache.clear()
            return _profile_photo_cache[cache_key]
        except Exception as e:
            logger.warning(f"Error downloading profile photo for user {user_id}: {e}")
            _profile_photo_cache[cache_key] = None
            return None


# Helper: Download media with disk caching
async def download_media(message, thumbnail_only: bool = False):
    media_type = message.media.__class__.__name__
    media_id = f"{message.id}_{media_type}"
    cache_file = os.path.join(_media_cache_dir, f"{hashlib.md5(media_id.encode()).hexdigest()}.bin")

    if os.path.exists(cache_file):
        logger.debug(f"Returning cached media {media_id}")
        with open(cache_file, "rb") as f:
            media_bytes = BytesIO(f.read())
    else:
        media_bytes = await client.download_media(message.media, file=BytesIO(), thumb=thumbnail_only)
        if media_bytes.getbuffer().nbytes > 10 * 1024 * 1024:  # Limit to 10 MB
            logger.warning(f"Media {media_id} too large, skipping")
            raise ValueError("Media too large")
        with open(cache_file, "wb") as f:
            f.write(media_bytes.getvalue())
        logger.debug(f"Cached media {media_id} to disk")

    return media_bytes, media_type


# Helper: List last messages from group chats with filters and detailed user info
async def get_last_messages(chat_id: int, limit: int = 10, offset_id: int = 0, query: str = None,
                            start_date: str = None, end_date: str = None):
    try:
        logger.info(f"Fetching messages for chat {chat_id} with limit {limit}, offset {offset_id}")
        messages = []
        semaphore = asyncio.Semaphore(3)  # Reduced to 3 for safety
        async for msg in client.iter_messages(chat_id, limit=limit, offset_id=offset_id):
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
                profile_photo = None
                if sender and hasattr(sender, 'id'):
                    profile_photo = await download_profile_photo(sender.id, semaphore)
                user_info = {
                    "id": sender.id if sender else "Unknown",
                    "first_name": getattr(sender, "first_name", "Unknown"),
                    "last_name": getattr(sender, "last_name", ""),
                    "username": getattr(sender, "username", "No username"),
                    "phone": getattr(sender, "phone", "Hidden"),
                    "profile_photo": profile_photo
                }
                content = msg.text if msg.text else f"[Media: {msg.media.__class__.__name__}]"
                messages.append({"content": content, "date": msg.date, "id": msg.id, "user": user_info})
        next_offset_id = messages[-1]["id"] if messages else offset_id
        logger.info(f"Fetched {len(messages)} messages for chat {chat_id}")
        return {"messages": messages, "next_offset_id": next_offset_id}
    except Exception as e:
        logger.error(f"Error fetching messages for chat {chat_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch messages")


# Helper: List all users in a chat with proper pagination
async def get_chat_users(chat_id: int, limit: int = 100, offset: int = 0):
    try:
        logger.info(f"Fetching users for chat {chat_id} with limit {limit}, offset {offset}")
        users = []
        semaphore = asyncio.Semaphore(3)  # Reduced to 3 for safety
        entity = await client.get_entity(chat_id)
        logger.debug(f"Entity type: {type(entity).__name__}")
        if isinstance(entity, Channel):
            participants = await client(GetParticipantsRequest(
                channel=entity,
                filter=ChannelParticipantsSearch(''),
                offset=offset,
                limit=limit + 1,
                hash=0
            ))
            logger.debug(f"Fetched {len(participants.users)} participants")
            photo_tasks = [download_profile_photo(user.id, semaphore) for user in participants.users[:limit]]
            profile_photos = await asyncio.gather(*photo_tasks, return_exceptions=True)
            for user, profile_photo in zip(participants.users[:limit], profile_photos):
                user_info = {
                    "id": user.id,
                    "first_name": getattr(user, "first_name", "Unknown"),
                    "last_name": getattr(user, "last_name", ""),
                    "username": getattr(user, "username", "No username"),
                    "phone": getattr(user, "phone", "Hidden"),
                    "profile_photo": profile_photo if isinstance(profile_photo, str) else None
                }
                users.append(user_info)
            has_next = len(participants.users) > limit
            next_offset_id = offset + len(users) if has_next else None
            logger.info(f"Fetched {len(users)} users for channel {chat_id}, has_next: {has_next}")
        else:
            count = 0
            async for user in client.iter_participants(chat_id, limit=limit + 1):
                if offset > 0:
                    offset -= 1
                    continue
                if count >= limit:
                    break
                profile_photo = await download_profile_photo(user.id, semaphore)
                user_info = {
                    "id": user.id,
                    "first_name": getattr(user, "first_name", "Unknown"),
                    "last_name": getattr(user, "last_name", ""),
                    "username": getattr(user, "username", "No username"),
                    "phone": getattr(user, "phone", "Hidden"),
                    "profile_photo": profile_photo
                }
                users.append(user_info)
                count += 1
            has_next = count >= limit
            next_offset_id = offset + len(users) if has_next else None
            logger.info(f"Fetched {len(users)} users for group {chat_id}, has_next: {has_next}")
        return {"users": users, "next_offset_id": next_offset_id, "error": None}
    except ChatAdminRequiredError:
        logger.error(f"ChatAdminRequiredError for chat {chat_id}")
        return {"users": [], "next_offset_id": None,
                "error": "Access to the user list is restricted. Administrative rights are required."}
    except Exception as e:
        logger.error(f"Error fetching users for chat {chat_id}: {e}")
        return {"users": [], "next_offset_id": None, "error": f"Failed to fetch users: {str(e)}"}


# Helper: List all media files in a chat with full content
async def get_chat_media(chat_id: int, limit: int = 20, offset_id: int = 0, thumbnail_only: bool = True):
    try:
        logger.info(f"Fetching media for chat {chat_id} with limit {limit}, offset {offset_id}")
        clean_media_cache()  # Clean old media files
        media_files = []
        async for msg in client.iter_messages(chat_id, limit=limit + 1, offset_id=offset_id, filter=None):
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
                logger.debug(f"Processing media {msg.id} of type {media_type}")
                media_data = {}
                try:
                    media_bytes, media_type = await download_media(msg, thumbnail_only)
                    if isinstance(msg.media, MessageMediaPhoto):
                        media_data = {
                            "type": "image",
                            "base64": f"data:image/jpeg;base64,{base64.b64encode(media_bytes.getvalue()).decode('utf-8')}",
                            "filename": None,
                            "mime_type": "image/jpeg"
                        }
                    elif isinstance(msg.media, MessageMediaDocument):
                        is_video = any(
                            isinstance(attr, DocumentAttributeVideo) for attr in msg.media.document.attributes)
                        is_audio = any(
                            isinstance(attr, DocumentAttributeAudio) for attr in msg.media.document.attributes)
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
                        elif is_audio:
                            media_data = {
                                "type": "audio",
                                "base64": f"data:audio/mpeg;base64,{base64.b64encode(media_bytes.getvalue()).decode('utf-8')}",
                                "filename": filename,
                                "mime_type": "audio/mpeg"
                            }
                        else:
                            media_data = {
                                "type": "document",
                                "base64": f"data:{mime_type or 'application/octet-stream'};base64,{base64.b64encode(media_bytes.getvalue()).decode('utf-8')}",
                                "filename": filename,
                                "mime_type": mime_type or "application/octet-stream"
                            }
                    else:
                        logger.warning(f"Unsupported media type {media_type} for message {msg.id}")
                        media_data = {
                            "type": "unsupported",
                            "base64": None,
                            "filename": None,
                            "mime_type": None
                        }
                except Exception as e:
                    logger.warning(f"Error downloading media {msg.id}: {e}")
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
            if len(media_files) >= limit:
                break
        next_offset_id = media_files[-1]["id"] if media_files and len(media_files) == limit else None
        logger.info(f"Fetched {len(media_files)} media files for chat {chat_id}")
        return {"media_files": media_files[:limit], "next_offset_id": next_offset_id}
    except Exception as e:
        logger.error(f"Error fetching media for chat {chat_id}: {e}")
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
            logger.info(f"Group Message from {user_info} in {event.chat_id}: {text}")


# Reset chat selection
@app.get("/reset-chat", response_class=RedirectResponse)
async def reset_chat(request: Request):
    request.session.pop("chat_id", None)
    logger.info("Chat selection reset")
    return RedirectResponse(url="/", status_code=303)


# Main chat UI with search and date filters for group chats
@app.get("/", response_class=HTMLResponse)
async def form(
        request: Request,
        chat_id: int = None,
        query: str = None,
        start_date: str = None,
        end_date: str = None,
        offset_id: int = Query(default=0, ge=0),
        limit: int = Query(default=10, ge=1, le=100),
        tab: str = Query(default="messages")
):
    logger.info(f"Handling request for tab {tab}, chat_id {chat_id}, offset {offset_id}, limit {limit}")
    if chat_id is not None:
        request.session["chat_id"] = chat_id
    elif "chat_id" in request.session:
        chat_id = request.session["chat_id"]

    groups = await get_group_chats()
    if chat_id is not None:
        valid_chat_ids = [group["id"] for group in groups]
        if chat_id not in valid_chat_ids:
            logger.warning(f"Invalid chat_id {chat_id}, resetting session")
            request.session.pop("chat_id", None)
            chat_id = None

    messages = []
    next_offset_id = offset_id
    users_data = {"users": [], "next_offset_id": offset_id, "error": None}
    media_files = []
    media_next_offset_id = offset_id
    if chat_id:
        if tab == "messages":
            result = await get_last_messages(chat_id, limit=limit, offset_id=offset_id, query=query,
                                             start_date=start_date,
                                             end_date=end_date)
            messages = result["messages"]
            next_offset_id = result["next_offset_id"]
        elif tab == "users":
            users_data = await get_chat_users(chat_id, limit=limit, offset=offset_id)
            next_offset_id = users_data["next_offset_id"]
        elif tab == "media":
            result = await get_chat_media(chat_id, limit=limit, offset_id=offset_id, thumbnail_only=True)
            media_files = result["media_files"]
            media_next_offset_id = result["next_offset_id"]
    logger.info(
        f"Rendering template for tab {tab} with {len(messages)} messages, {len(users_data['users'])} users, {len(media_files)} media files")
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
        "offset_id": offset_id,
        "next_offset_id": next_offset_id,
        "media_next_offset_id": media_next_offset_id,
        "limit": limit,
        "tab": tab
    })