from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.sql import func

from app.database import Base


class ExecutionTask(Base):
    __tablename__ = "execution_tasks"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    service_id = Column(Integer, ForeignKey("government_services.id"), nullable=False, index=True)
    state = Column(String(50), nullable=False, default="DRAFT", index=True)
    ready_for_dispatch = Column(Boolean, nullable=False, default=False)
    citizen_approved = Column(Boolean, nullable=False, default=False)
    manifest_json = Column(Text, nullable=False, default="{}")
    form_payload_json = Column(Text, nullable=False, default="{}")
    blocker = Column(Text, nullable=True)
    tracking_number = Column(String(255), nullable=True)
    submission_evidence = Column(String(512), nullable=True)
    last_error = Column(Text, nullable=True)
    progress_percent = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now(), server_default=func.now())
