from sqlalchemy import Column, Integer, String, Float, ForeignKey, Date, DateTime
from sqlalchemy.sql import func
from app.database import Base


class CitizenProfile(Base):
    __tablename__ = "citizen_profiles"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False)
    full_name = Column(String(255), nullable=False)
    dob = Column(Date, nullable=True)
    gender = Column(String(20), nullable=True, default="other")  # "male", "female", "transgender", "other"
    age = Column(Integer, nullable=True)
    state = Column(String(100), nullable=False)
    district = Column(String(100), nullable=False)
    annual_family_income = Column(Float, nullable=False)
    phone_number = Column(String(20), nullable=False)
    nationality = Column(
        String(50),
        nullable=False,
        default="Indian",
        server_default="Indian"
    )

    # Occupation classifier: 'student' | 'worker' | 'unemployed' | 'other'
    occupation_type = Column(String(50), nullable=False, default="student")

    # Student-specific fields
    education_level = Column(String(100), nullable=True)
    institution_name = Column(String(255), nullable=True)
    course_or_degree = Column(String(100), nullable=True)

    # Worker-specific fields
    employment_type = Column(String(100), nullable=True)
    industry_or_sector = Column(String(100), nullable=True)
    employer_name = Column(String(255), nullable=True)

    updated_at = Column(DateTime(timezone=True), onupdate=func.now(), server_default=func.now())