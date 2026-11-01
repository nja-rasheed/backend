import json
from sqlalchemy.orm import Session
from app.models.service import GovernmentService

OFFICIAL_SERVICES = [
    {
        "service_code": "NSP_CENTRAL_SECTOR",
        "name": "Central Sector Scheme of Scholarships for College and University Students",
        "category": "education",
        "portal_url": "https://scholarships.gov.in/",
        "description": "Financial assistance by the Department of Higher Education for meritorious students from low-income families pursuing regular degree courses.",
        "eligibility_rules": {
            "min_marks_percentile": 80.0,
            "max_annual_family_income": 450000.0,
            "education_level": ["Undergraduate", "Postgraduate"],
            "target_state": "All India"
        },
        "required_documents": [
            "income_certificate",
            "marksheet_12th",
            "bonafide_student_certificate",
            "bank_passbook"
        ]
    },
    {
        "service_code": "NSP_POST_MATRIC_SC_ST",
        "name": "Post Matric Scholarships Scheme for Minorities & Reserved Categories",
        "category": "education",
        "portal_url": "https://scholarships.gov.in/",
        "description": "Scholarship to support higher education studies from Class 11 to Post Doctoral level.",
        "eligibility_rules": {
            "max_annual_family_income": 250000.0,
            "education_level": ["11th", "12th", "Diploma", "Undergraduate", "Postgraduate"],
            "target_state": "All India"
        },
        "required_documents": [
            "income_certificate",
            "community_certificate",
            "previous_year_marksheet",
            "bonafide_student_certificate"
        ]
    },
    {
        "service_code": "CPGRAMS_PUBLIC_GRIEVANCE",
        "name": "CPGRAMS Centralized Public Grievance Redress and Monitoring System",
        "category": "grievance",
        "portal_url": "https://pgportal.gov.in/",
        "description": "Government of India portal to lodge citizen grievances regarding central and state government departments, civic infrastructure, and services.",
        "eligibility_rules": {
            "user_type": ["citizen", "resident"],
            "target_state": "All India"
        },
        "required_documents": [
            "grievance_proof_document"
        ]
    }
]


def seed_government_services(db: Session):
    for item in OFFICIAL_SERVICES:
        existing = db.query(GovernmentService).filter(
            GovernmentService.service_code == item["service_code"]
        ).first()

        if not existing:
            service = GovernmentService(
                service_code=item["service_code"],
                name=item["name"],
                category=item["category"],
                portal_url=item["portal_url"],
                description=item["description"],
                eligibility_rules=json.dumps(item["eligibility_rules"]),
                required_documents=json.dumps(item["required_documents"]),
            )
            db.add(service)
    db.commit()