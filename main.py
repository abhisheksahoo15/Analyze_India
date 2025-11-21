from fastapi import FastAPI, Depends, BackgroundTasks, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse
from starlette.staticfiles import StaticFiles
from sqlmodel import Session, select
from datetime import datetime

from fastapi_mail import FastMail, MessageSchema, ConnectionConfig
from starlette_admin.contrib.sqlmodel import Admin
from starlette_admin.contrib.sqlmodel import ModelView

from database import Subscriber, create_db_and_tables, get_session, engine
from dotenv import load_dotenv
import os

load_dotenv()

# --- Configuration ---
# 1. Email Config
# Build a ConnectionConfig-compatible dict. Different fastapi-mail
# versions expect `MAIL_STARTTLS` and `MAIL_SSL_TLS` instead of
# `MAIL_TLS`/`MAIL_SSL`. Convert the .env flags accordingly and
# only pass the accepted keys to ConnectionConfig.
conf_kwargs = {
    "MAIL_USERNAME": os.getenv("MAIL_USERNAME"),
    "MAIL_PASSWORD": os.getenv("MAIL_PASSWORD"),
    "MAIL_FROM": os.getenv("MAIL_FROM"),
    "MAIL_PORT": int(os.getenv("MAIL_PORT") or 587),
    "MAIL_SERVER": os.getenv("MAIL_SERVER"),
    # fastapi-mail expects starttls / ssl keys in some versions
    "MAIL_STARTTLS": os.getenv("MAIL_TLS") == 'True',
    "MAIL_SSL_TLS": os.getenv("MAIL_SSL") == 'True',
    "USE_CREDENTIALS": os.getenv("USE_CREDENTIALS") == 'True',
    "VALIDATE_CERTS": os.getenv("VALIDATE_CERTS") == 'True',
}

conf = ConnectionConfig(**conf_kwargs)

# 2. Basic Admin Authentication (lightweight compatibility layer)
class CustomAuthBackend:
    """Simple auth backend compatible with Starlette-Admin expectations.

    This class intentionally does not subclass any package class because
    different `starlette-admin` versions expose different base classes.
    It exposes a `login_url` attribute and an async `authenticate(request)`
    method which the admin will call.
    """
    def __init__(self, login_url: str = "/admin/login"):
        self.login_url = login_url

    async def authenticate(self, request: Request):
        # Basic auth extracted from the Authorization header
        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Basic "):
            return False

        try:
            import base64
            encoded_credentials = auth_header.split(" ")[1]
            decoded_credentials = base64.b64decode(encoded_credentials).decode("utf-8")
            username, password = decoded_credentials.split(":", 1)
        except Exception:
            return False

        # TEMPORARY AUTH CHECK - REPLACE THIS LOGIC LATER WITH HASHED PASSWORDS
        if username == "admin_analyze" and password == "strong_password_123":
            return True
        return False


# --- App Initialization ---
app = FastAPI(title="Analyze India Backend API")
app.mount("/static", StaticFiles(directory="./static"), name="static")

# Enable CORS for frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:8000", "http://localhost:8000", "http://127.0.0.1:5500", "http://localhost:5500"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Admin Panel Setup ---
# Note: different `starlette-admin` versions accept different auth
# configuration arguments. To remain compatible with multiple
# versions, construct the Admin without passing `auth_backend` here.
admin = Admin(engine, title="Analyze India Admin")
admin.add_view(ModelView(Subscriber))

# Attach admin to the FastAPI app. Different `starlette-admin` versions
# expose different helper methods; try common ones with fallbacks.
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
                print(f"Attempt to attach admin using {method_name} failed: {e}")
    if not attached:
        print("Warning: Could not find a supported method to attach Admin to the app.")
        print("You may need to upgrade/downgrade starlette-admin or attach routes manually.")


# --- Email Sending ---
async def send_welcome_email(recipient_email: str):
    """Sends a thank you email to the new subscriber."""
    message = MessageSchema(
        subject="Welcome to Analyze India 🚀 — Your Insights Start Now!",
        recipients=[recipient_email],
        body = f"""
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
            """
            ,
        subtype="html"
    )
    fm = FastMail(conf)
    await fm.send_message(message)


# --- API Endpoints ---

@app.on_event("startup")
def on_startup():
    """Runs on application startup to ensure tables exist."""
    create_db_and_tables()

@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    """Serves the index.html file from the static folder."""
    try:
        # NOTE: Your browser should automatically fetch static/index.html 
        # But this endpoint ensures the base URL works.
        with open("static/index.html", "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    except FileNotFoundError:
        return HTMLResponse("<h1>Frontend File Not Found!</h1><p>Please ensure 'index.html' is in a 'static' directory.</p>")


@app.post("/api/subscribe/")
async def subscribe_user(email: dict, background_tasks: BackgroundTasks, db: Session = Depends(get_session)):
    """Handles new user subscriptions."""
    user_email = email.get('email')
    if not user_email:
        raise HTTPException(status_code=422, detail="Email is required.")

    try:
        existing_subscriber = db.exec(select(Subscriber).where(Subscriber.email == user_email)).first()
        if existing_subscriber:
            raise HTTPException(status_code=400, detail="Email already subscribed.")
        
        new_subscriber = Subscriber(email=user_email)
        db.add(new_subscriber)
        db.commit()
        db.refresh(new_subscriber)

        # Send email asynchronously
        background_tasks.add_task(send_welcome_email, user_email)

        return {"message": "Subscription successful. A welcome email is being sent."}

    except HTTPException as e:
        raise e
    except Exception as e:
        print(f"Error during subscription: {e}")
        raise HTTPException(status_code=500, detail="Server error during subscription.")
