from sqlalchemy import Column, Integer, String, DateTime, Text, JSON, Float, Enum, ForeignKey
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
import enum

from src.database.base import Base

class BookingStatus(enum.Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"

class Booking(Base):
    __tablename__ = "bookings"
    
    id = Column(Integer, primary_key=True, index=True)
    user_email = Column(String(255), nullable=False, index=True)
    user_name = Column(String(100), nullable=False)
    course_level = Column(String(10), nullable=False)
    preferred_date = Column(DateTime, nullable=False)
    preferred_time_slot = Column(String(10), nullable=True)
    phone_number = Column(String(20), nullable=False)
    notes = Column(Text, nullable=True)
    
    status = Column(Enum(BookingStatus), default=BookingStatus.PENDING, index=True)
    progress_percentage = Column(Float, default=0.0)
    current_step = Column(String(255), nullable=True)
    error_message = Column(Text, nullable=True)
    result_data = Column(JSON, nullable=True)
    
    created_by_admin_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    completed_at = Column(DateTime(timezone=True), nullable=True)
    
    automation_logs = relationship("AutomationLog", back_populates="booking")
    created_by_admin = relationship("User", foreign_keys=[created_by_admin_id])