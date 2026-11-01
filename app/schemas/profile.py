
from datetime import date
from typing import Optional, Literal
from pydantic import BaseModel, Field, field_serializer


class CitizenProfileCreate(BaseModel):
    full_name: str = Field(..., example="Rahul Sharma")
    dob: Optional[date] = Field(None, example="2004-05-15")
    age: Optional[int] = Field(None, ge=0, le=150, example=22)
    gender: Literal["male", "female", "transgender", "other"] = Field(
        default="other", example="male"
    )
    state: str = Field(..., example="Tamil Nadu")
    district: str = Field(..., example="Tiruchirappalli")
    annual_family_income: float = Field(..., example=180000.0)
    phone_number: str = Field(..., example="9876543210")
    nationality: str = Field(default="Indian", example="Indian")

    # Occupation selector
    occupation_type: Literal["student", "worker", "unemployed", "other"] = Field(
        default="student", example="student"
    )

    # Student fields
    education_level: Optional[str] = Field(None, example="Undergraduate")
    institution_name: Optional[str] = Field(None, example="National Institute of Technology")
    course_or_degree: Optional[str] = Field(None, example="B.Tech Computer Science")

    # Worker fields
    employment_type: Optional[str] = Field(None, example="Salaried")
    industry_or_sector: Optional[str] = Field(None, example="Information Technology")
    employer_name: Optional[str] = Field(None, example="TCS")


class CitizenProfileUpdate(BaseModel):
    full_name: Optional[str] = None
    dob: Optional[date] = None
    age: Optional[int] = Field(None, ge=0, le=150)
    gender: Optional[Literal["male", "female", "transgender", "other"]] = None
    state: Optional[str] = None
    district: Optional[str] = None
    annual_family_income: Optional[float] = None
    phone_number: Optional[str] = None
    occupation_type: Optional[Literal["student", "worker", "unemployed", "other"]] = None

    # Student fields
    education_level: Optional[str] = None
    institution_name: Optional[str] = None
    course_or_degree: Optional[str] = None

    # Worker fields
    employment_type: Optional[str] = None
    industry_or_sector: Optional[str] = None
    employer_name: Optional[str] = None


class CitizenProfileResponse(BaseModel):
    id: int
    user_id: int
    full_name: str
    dob: Optional[date]
    age: Optional[int]
    gender: Optional[str]
    state: str
    district: str
    annual_family_income: float
    phone_number: str
    nationality: str
    occupation_type: str

    # Student fields
    education_level: Optional[str]
    institution_name: Optional[str]
    course_or_degree: Optional[str]

    # Worker fields
    employment_type: Optional[str]
    industry_or_sector: Optional[str]
    employer_name: Optional[str]

    class Config:
        from_attributes = True

    @field_serializer("dob")
    def serialize_dob(self, value: Optional[date]) -> Optional[str]:
        return value.strftime("%d/%m/%Y") if value else None
