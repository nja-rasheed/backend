from sqlalchemy import Column, Integer, String, ForeignKey, DateTime
from sqlalchemy.sql import func
from app.database import Base


class Application(Base):
    __tablename__ = "applications"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    service_id = Column(Integer, ForeignKey("government_services.id"), nullable=False)
    status = Column(String(50), default="initiated", nullable=False)
    # initiated, eligibility_checked, documents_pending, ready_for_review,
    # awaiting_user_approval, submitted, failed, tracking_available
    acknowledgement_number = Column(String(255), nullable=True)
    submission_evidence = Column(String(512), nullable=True)  # URL or path to receipt screenshot/pdf
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now(), server_default=func.now())