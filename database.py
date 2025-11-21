from sqlmodel import SQLModel, create_engine, Session, Field
from typing import Optional
from datetime import datetime
import os
from dotenv import load_dotenv

load_dotenv()
# Retrieves the SQLite connection string. SQLite creates the database file automatically.
DATABASE_URL = os.getenv("DATABASE_URL") or "sqlite:///./sql_app.db"

engine = create_engine(DATABASE_URL, echo=True)

def create_db_and_tables():
    """Initializes the database and creates all tables."""
    SQLModel.metadata.create_all(engine)

def get_session():
    """Dependency for FastAPI to get a database session."""
    with Session(engine) as session:
        yield session

class Subscriber(SQLModel, table=True):
    """The table for storing subscriber emails."""
    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(unique=True, index=True)
    is_active: bool = Field(default=True)
    date_subscribed: datetime = Field(default_factory=datetime.utcnow) 
    
    __admin_label__ = "Subscriber Management" 

class User(SQLModel, table=True):
    """Model for Admin Panel users (login) - for future use."""
    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(unique=True, index=True)
    hashed_password: str
    is_admin: bool = Field(default=False)
