"""
Booking automation REST endpoints with full CRUD operations,
background task scheduling, and real-time status updates.
"""

from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Query, status
from pydantic import BaseModel, Field

from src.api.v1.schemas.booking import (
    BookingRequest, BookingResponse, BookingStatusResponse,
    BookingSlotResponse, BookingListResponse
)
from src.api.deps import get_current_admin_user, get_playwright_manager, get_retry_engine
from src.services.automation.booking_engine import BookingEngine
from src.services.task_queue.background_tasks import BackgroundTaskManager
from src.services.notification.notifier import Notifier
from src.core.exceptions import AutomationError, NotFoundError
from src.core.config import settings

router = APIRouter(prefix="/bookings", tags=["Bookings"])

class BookingCreateRequest(BaseModel):
    user_email: str = Field(..., regex=r'^[^@]+@[^@]+\.[^@]+$')
    user_name: str = Field(..., min_length=2, max_length=100)
    course_level: str = Field(..., regex="^(A1|A2|B1|B2|C1|C2)$")
    preferred_date: str = Field(..., description="ISO date format YYYY-MM-DD")
    preferred_time_slot: Optional[str] = Field(None, regex="^(09:00|11:00|14:00|16:00)$")
    phone_number: str = Field(..., regex=r'^\+233[0-9]{9}$')
    notes: Optional[str] = None

@router.post("/", response_model=BookingResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_booking(
    request: BookingCreateRequest,
    background_tasks: BackgroundTasks,
    admin: dict = Depends(get_current_admin_user),
    playwright_manager = Depends(get_playwright_manager),
    retry_engine = Depends(get_retry_engine),
):
    """
    Create a new booking automation task.
    Returns task ID immediately, processes in background.
    """
    booking_engine = BookingEngine(playwright_manager, retry_engine)
    
    # Create booking record in database
    from src.repositories.booking_repository import BookingRepository
    repo = BookingRepository()
    
    booking_data = {
        "user_email": request.user_email,
        "user_name": request.user_name,
        "course_level": request.course_level,
        "preferred_date": request.preferred_date,
        "preferred_time_slot": request.preferred_time_slot,
        "phone_number": request.phone_number,
        "notes": request.notes,
        "status": "pending",
        "created_by_admin_id": admin.get("id"),
    }
    
    booking = await repo.create(booking_data)
    
    # Schedule background task
    task_manager = BackgroundTaskManager()
    await task_manager.schedule_booking(
        booking_id=booking.id,
        user_email=request.user_email,
        course_level=request.course_level,
        preferred_date=request.preferred_date,
        preferred_time_slot=request.preferred_time_slot
    )
    
    # Send notification
    await Notifier.notify_booking_created(booking.id, request.user_email)
    
    return BookingResponse(
        id=booking.id,
        status="pending",
        message="Booking automation scheduled successfully",
        estimated_completion_time="5-10 minutes"
    )

@router.get("/{booking_id}", response_model=BookingStatusResponse)
async def get_booking_status(
    booking_id: int,
    admin: dict = Depends(get_current_admin_user),
):
    """Get detailed status of a booking automation task."""
    from src.repositories.booking_repository import BookingRepository
    repo = BookingRepository()
    
    booking = await repo.get_by_id(booking_id)
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    
    return BookingStatusResponse(
        id=booking.id,
        status=booking.status,
        progress=booking.progress_percentage or 0,
        current_step=booking.current_step,
        error_message=booking.error_message,
        created_at=booking.created_at,
        updated_at=booking.updated_at,
        result_details=booking.result_data
    )

@router.get("/", response_model=BookingListResponse)
async def list_bookings(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    status_filter: Optional[str] = None,
    admin: dict = Depends(get_current_admin_user),
):
    """List all bookings with pagination and filtering."""
    from src.repositories.booking_repository import BookingRepository
    repo = BookingRepository()
    
    bookings, total = await repo.get_paginated(
        page=page,
        limit=limit,
        status=status_filter
    )
    
    return BookingListResponse(
        items=bookings,
        total=total,
        page=page,
        limit=limit,
        pages=(total + limit - 1) // limit
    )

@router.post("/{booking_id}/cancel", response_model=BookingResponse)
async def cancel_booking(
    booking_id: int,
    admin: dict = Depends(get_current_admin_user),
):
    """Cancel a pending or in-progress booking automation."""
    from src.repositories.booking_repository import BookingRepository
    from src.services.task_queue.background_tasks import BackgroundTaskManager
    
    repo = BookingRepository()
    booking = await repo.get_by_id(booking_id)
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    
    if booking.status not in ["pending", "in_progress"]:
        raise HTTPException(status_code=400, detail=f"Cannot cancel booking with status {booking.status}")
    
    # Cancel background task
    task_manager = BackgroundTaskManager()
    await task_manager.cancel_task(f"booking_{booking_id}")
    
    # Update status
    await repo.update(booking_id, {"status": "cancelled"})
    
    await Notifier.notify_booking_cancelled(booking_id, booking.user_email)
    
    return BookingResponse(
        id=booking.id,
        status="cancelled",
        message="Booking automation cancelled successfully"
    )

@router.post("/slots/check", response_model=List[BookingSlotResponse])
async def check_available_slots(
    course_level: str,
    date_from: str,
    date_to: str,
    admin: dict = Depends(get_current_admin_user),
):
    """Check available booking slots for a date range."""
    from src.services.automation.booking_engine import BookingEngine
    playwright_manager = ...  # Get from dependency
    
    engine = BookingEngine(playwright_manager, await get_retry_engine())
    slots = await engine.check_availability(
        course_level=course_level,
        start_date=date_from,
        end_date=date_to
    )
    
    return [BookingSlotResponse(**slot) for slot in slots]