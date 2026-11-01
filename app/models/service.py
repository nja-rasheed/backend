from sqlalchemy import Column, Integer, String, Text, ForeignKey, DateTime, UniqueConstraint
from sqlalchemy.sql import func
from app.database import Base


class GovernmentService(Base):
    __tablename__ = "government_services"

    id = Column(Integer, primary_key=True, index=True)
    service_code = Column(String(100), unique=True, index=True, nullable=False)
    name = Column(String(255), nullable=False)
    category = Column(String(100), nullable=False)  # education, grievance, civic
    governance_tier = Column(String(50), default="state")
    portal_url = Column(String(512), nullable=False)
    description = Column(Text, nullable=False)
    eligibility_rules = Column(Text, nullable=False)  # JSON string of criteria
    required_documents = Column(Text, nullable=False)  # JSON array string
    required_form_fields = Column(Text, default="[]", nullable=False)  # JSON array string


class UserDiscoveredService(Base):
    __tablename__ = "user_discovered_services"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )
    service_id = Column(
        Integer,
        ForeignKey("government_services.id", ondelete="CASCADE"),
        nullable=False
    )
    discovered_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("user_id", "service_id", name="uq_user_service"),
    )