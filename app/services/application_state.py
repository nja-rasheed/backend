from enum import Enum


class ApplicationState(str, Enum):
    DRAFT = "DRAFT"
    DOCUMENT_VALIDATION = "DOCUMENT_VALIDATION"
    ELIGIBILITY_VERIFICATION = "ELIGIBILITY_VERIFICATION"
    APPLICATION_PREPARATION = "APPLICATION_PREPARATION"
    CITIZEN_REVIEW = "CITIZEN_REVIEW"
    CITIZEN_APPROVAL = "CITIZEN_APPROVAL"
    PORTAL_NAVIGATION = "PORTAL_NAVIGATION"
    FORM_COMPLETION = "FORM_COMPLETION"
    CITIZEN_FINAL_CONFIRMATION = "CITIZEN_FINAL_CONFIRMATION"
    APPLICATION_SUBMISSION = "APPLICATION_SUBMISSION"
    APPLICATION_TRACKING = "APPLICATION_TRACKING"
    COMPLETED = "COMPLETED"
    READY_FOR_DISPATCH = "READY_FOR_DISPATCH"
    DISPATCHED = "DISPATCHED"
    WAITING_FOR_CITIZEN_INTERVENTION = "WAITING_FOR_CITIZEN_INTERVENTION"
    CITIZEN_APPROVED = "CITIZEN_APPROVED"
    SUBMITTED = "SUBMITTED"
    FAILED = "FAILED"


STATE_TRANSITIONS = {
    ApplicationState.DRAFT: {ApplicationState.DOCUMENT_VALIDATION},
    ApplicationState.DOCUMENT_VALIDATION: {ApplicationState.ELIGIBILITY_VERIFICATION},
    ApplicationState.ELIGIBILITY_VERIFICATION: {ApplicationState.APPLICATION_PREPARATION},
    ApplicationState.APPLICATION_PREPARATION: {ApplicationState.CITIZEN_REVIEW},
    ApplicationState.CITIZEN_REVIEW: {ApplicationState.CITIZEN_APPROVAL},
    ApplicationState.CITIZEN_APPROVAL: {ApplicationState.PORTAL_NAVIGATION},
    ApplicationState.PORTAL_NAVIGATION: {ApplicationState.FORM_COMPLETION},
    ApplicationState.FORM_COMPLETION: {ApplicationState.CITIZEN_FINAL_CONFIRMATION},
    ApplicationState.CITIZEN_FINAL_CONFIRMATION: {ApplicationState.APPLICATION_SUBMISSION},
    ApplicationState.APPLICATION_SUBMISSION: {ApplicationState.APPLICATION_TRACKING},
    ApplicationState.APPLICATION_TRACKING: {ApplicationState.COMPLETED},
    ApplicationState.COMPLETED: set(),
    ApplicationState.READY_FOR_DISPATCH: {
        ApplicationState.CITIZEN_APPROVED,
        ApplicationState.DISPATCHED,
    },
    ApplicationState.DISPATCHED: {
        ApplicationState.PORTAL_NAVIGATION,
        ApplicationState.WAITING_FOR_CITIZEN_INTERVENTION,
        ApplicationState.FAILED,
    },
    ApplicationState.WAITING_FOR_CITIZEN_INTERVENTION: {
        ApplicationState.DISPATCHED,
        ApplicationState.CITIZEN_APPROVED,
        ApplicationState.FAILED,
    },
    ApplicationState.CITIZEN_APPROVED: {ApplicationState.DISPATCHED},
    ApplicationState.SUBMITTED: {ApplicationState.COMPLETED},
    ApplicationState.FAILED: set(),
}


def can_transition(current: ApplicationState, target: ApplicationState) -> bool:
    return target in STATE_TRANSITIONS.get(current, set())
