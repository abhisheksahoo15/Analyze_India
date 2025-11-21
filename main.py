from fastapi import FastAPI, Depends, BackgroundTasks, HTTPException, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse
from starlette.staticfiles import StaticFiles
from sqlmodel import Session, select
from datetime import datetime
import threading
import asyncio
import time
import json
import os
import logging

# optional: tweepy only used if you plan to stream from Twitter
try:
    import tweepy
except Exception:
    tweepy = None

from fastapi_mail import FastMail, MessageSchema, ConnectionConfig
# starlette-admin imports may fail at runtime depending on versions
try:
    from starlette_admin.contrib.sqlmodel import Admin, ModelView
except Exception:
    Admin = None
    ModelView = None

from database import Subscriber, create_db_and_tables, get_session, engine
from dotenv import load_dotenv

load_dotenv()

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("analyzeindia")

# Read environment variables (works for local .env and Azure App Settings)
BEARER = os.getenv("TWITTER_BEARER_TOKEN")
MAIL_USERNAME = os.getenv("MAIL_USERNAME")
MAIL_PASSWORD = os.getenv("MAIL_PASSWORD")
MAIL_FROM = os.getenv("MAIL_FROM")
MAIL_PORT = int(os.getenv("MAIL_PORT") or 587)
MAIL_SERVER = os.getenv("MAIL_SERVER")
MAIL_TLS = os.getenv("MAIL_TLS", "True").lower() == 'true'
MAIL_SSL = os.getenv("MAIL_SSL", "False").lower() == 'true'
USE_CREDENTIALS = os.getenv("USE_CREDENTIALS", "True").lower() == 'true'
VALIDATE_CERTS = os.getenv("VALIDATE_CERTS", "True").lower() == 'true'

# --- Configuration for FastAPI-Mail ---
conf = None
conf_kwargs = {
    "MAIL_USERNAME": MAIL_USERNAME,
    "MAIL_PASSWORD": MAIL_PASSWORD,
    "MAIL_FROM": MAIL_FROM,
    "MAIL_PORT": MAIL_PORT,
    "MAIL_SERVER": MAIL_SERVER,
    # convert to keys some fastapi-mail versions expect
    "MAIL_STARTTLS": MAIL_TLS,
    "MAIL_SSL_TLS": MAIL_SSL,
    "USE_CREDENTIALS": USE_CREDENTIALS,
    "VALIDATE_CERTS": VALIDATE_CERTS,
}

# Validate required email settings and build ConnectionConfig safely
missing = [k for k, v in conf_kwargs.items() if k in ("MAIL_USERNAME", "MAIL_PASSWORD", "MAIL_FROM", "MAIL_SERVER") and not v]
if missing:
    logger.warning("Missing email settings: %s. Email sending will be disabled until you configure them.", missing)
else:
    try:
        conf = ConnectionConfig(**conf_kwargs)
        logger.info("Email ConnectionConfig initialized.")
    except Exception as e:
        logger.exception("Failed to build ConnectionConfig: %s", e)
        conf = None


# --- Basic Admin Authentication (lightweight) ---
class CustomAuthBackend:
    def __init__(self, login_url: str = "/admin/login"):
        self.login_url = login_url

    async def authenticate(self, request: Request):
        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Basic "):
            return False
        try:
            import base64
            encoded = auth_header.split(" ")[1]
            decoded = base64.b64decode(encoded).decode("utf-8")
            username, password = decoded.split(":", 1)
        except Exception:
            return False
        # replace with real auth in production
        return username == "admin_analyze" and password == "strong_password_123"


# --- App Initialization ---
app = FastAPI(title="Analyze India Backend API")
app.mount("/static", StaticFiles(directory="./static"), name="static")

# CORS: for production change allow_origins to your domain
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:8000", "http://localhost:8000", "http://127.0.0.1:5500", "http://localhost:5500", "https://analyzeiindia.azurewebsites.net"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Admin Panel Setup (optional) ---
if Admin and ModelView:
    try:
        admin = Admin(engine, title="Analyze India Admin")
        admin.add_view(ModelView(Subscriber))
        if hasattr(admin, "mount_to_app"):
            admin.mount_to_app(app)
        else:
            attached = False
            for method_name in ("init_app", "install", "register"):
                method = getattr(admin, method_name, None)
                if callable(method):
                    try:
                        method(app)
                        attached = True
                        break
                    except Exception as e:
                        logger.warning("Failed to attach admin using %s: %s", method_name, e)
            if not attached:
                logger.warning("Could not mount starlette-admin. Admin UI will be unavailable.")
    except Exception as e:
        logger.exception("Error initializing Admin: %s", e)
else:
    logger.info("starlette-admin not available; skipping admin mount.")


# --- Email sending helper ---
async def send_welcome_email(recipient_email: str):
    if not conf:
        logger.warning("Skipping email send because ConnectionConfig is not configured.")
        return

    message = MessageSchema(
        subject="Welcome to Analyze India 🚀 — Your Insights Start Now!",
        recipients=[recipient_email],
        body=f"""
            <p>Dear Subscriber,</p>

            <p>Thank you for joining <strong>Analyze India</strong>! Your subscription is now active, and you're officially part of a community driven by data, intelligence, and innovation.</p>

            <p>You will now receive:</p>
            <ul>
                <li>AI-powered reports tailored to your interests</li>
                <li>Deep insights into trends shaping India</li>
                <li>Exclusive early access to upcoming tools and features</li>
            </ul>

            <p>Welcome aboard—let’s decode the future together!</p>

            <p>Warm regards,<br>
            <strong>The Analyze India Team</strong></p>

            <p style="font-size:12px; color:#777;">
            This is an automated system-generated email. Please do not reply.
            </p>
        """,
        subtype="html",
    )

    try:
        fm = FastMail(conf)
        await fm.send_message(message)
        logger.info("Welcome email sent to %s", recipient_email)
    except Exception as e:
        logger.exception("Failed to send welcome email to %s: %s", recipient_email, e)


# --- API Endpoints & Startup ---
@app.on_event("startup")
async def on_startup():
    # create DB tables
    create_db_and_tables()

    # prepare shared queue for tweets
    app.state.tweet_queue = asyncio.Queue()

    # start broadcaster background task
    asyncio.create_task(tweet_broadcaster(app.state.tweet_queue))

    # start twitter stream thread or simulator
    if BEARER and tweepy:
        loop = asyncio.get_running_loop()
        t = threading.Thread(target=start_twitter_stream, args=(loop, app.state.tweet_queue, BEARER), daemon=True)
        t.start()
    else:
        asyncio.create_task(simulate_tweets(app.state.tweet_queue))


@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    try:
        with open("static/index.html", "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    except FileNotFoundError:
        return HTMLResponse("<h1>Frontend File Not Found!</h1><p>Please ensure 'index.html' is in a 'static' directory.</p>")


@app.post("/api/subscribe/")
async def subscribe_user(email: dict, background_tasks: BackgroundTasks, db: Session = Depends(get_session)):
    user_email = email.get('email')
    if not user_email:
        raise HTTPException(status_code=422, detail="Email is required.")

    try:
        existing = db.exec(select(Subscriber).where(Subscriber.email == user_email)).first()
        if existing:
            raise HTTPException(status_code=400, detail="Email already subscribed.")

        new_subscriber = Subscriber(email=user_email)
        db.add(new_subscriber)
        db.commit()
        db.refresh(new_subscriber)

        # send welcome email in background if configured
        if conf:
            background_tasks.add_task(send_welcome_email, user_email)
        else:
            logger.info("Subscription created but email not sent (email not configured).")

        return {"message": "Subscription successful. A welcome email is being sent (if configured)."}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error during subscription: %s", e)
        raise HTTPException(status_code=500, detail="Server error during subscription.")


# --- Tweet broadcasting / WebSocket ---
class TweetManager:
    def __init__(self):
        self.active: set[WebSocket] = set()

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active.add(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active.discard(websocket)

    async def broadcast(self, message: dict):
        text = json.dumps(message)
        remove = []
        for ws in list(self.active):
            try:
                await ws.send_text(text)
            except Exception:
                remove.append(ws)
        for ws in remove:
            self.disconnect(ws)


tweet_manager = TweetManager()


async def tweet_broadcaster(queue: asyncio.Queue):
    while True:
        msg = await queue.get()
        try:
            await tweet_manager.broadcast(msg)
        except Exception as e:
            logger.exception("Error broadcasting tweet: %s", e)


async def simulate_tweets(queue: asyncio.Queue):
    counter = 1
    while True:
        await asyncio.sleep(2)
        fake = {"id": f"sim-{counter}", "user": "@simulator", "text": f"Simulated tweet #{counter}", "sentiment": "Neutral"}
        await queue.put(fake)
        counter += 1


def start_twitter_stream(loop: asyncio.AbstractEventLoop, queue: asyncio.Queue, bearer: str):
    if not tweepy:
        logger.warning("tweepy not installed; cannot start Twitter stream.")
        return

    class MyStream(tweepy.StreamingClient):
        def on_tweet(self, tweet):
            try:
                msg = {"id": tweet.id, "text": tweet.text}
                loop.call_soon_threadsafe(queue.put_nowait, msg)
            except Exception as ex:
                logger.exception("Error in tweet callback: %s", ex)

    try:
        stream = MyStream(bearer, wait_on_rate_limit=True)
        stream.sample()
    except Exception as e:
        logger.exception("Twitter streaming error: %s", e)


@app.websocket('/ws/tweets')
async def websocket_tweets(ws: WebSocket):
    await tweet_manager.connect(ws)
    try:
        while True:
            # receive pings from client (we ignore content)
            await ws.receive_text()
    except Exception:
        tweet_manager.disconnect(ws)


@app.get('/health')
async def health_check():
    return {"status": "ok"}

