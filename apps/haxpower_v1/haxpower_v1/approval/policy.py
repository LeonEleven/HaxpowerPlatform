"""B06 policy for primary submissions and independent follow-up actions."""

RECEIPT = "Material Receipt"
ISSUE = "Material Issue"
TRANSFER = "Material Transfer"
COUNT = "Stock Count"
CORRECTION = "Correct Effective Inventory"
PRIMARY = frozenset((RECEIPT, ISSUE, TRANSFER, COUNT))

PENDING = "Pending Approval"
APPROVED = "Approved"
READY = "Ready to Dispatch"
REJECTED = "Rejected"

ORDINARY_SUBMISSION = "Global Inventory Approval"
NO_REPEAT = "No Repeat Approval"
CORRECTION_APPROVAL = "Specialist Correction Approval"
OPENING_SIGNOFF = "Opening Inventory Sign-off"


def requirement(action):
    """Integration contract for V1-14 and V1-17, independent of the global setting."""
    return {
        "Submit": ORDINARY_SUBMISSION,
        "Receive Transfer": NO_REPEAT,
        "Return Transfer": NO_REPEAT,
        CORRECTION: CORRECTION_APPROVAL,
        "Post Opening Inventory": OPENING_SIGNOFF,
    }[action]
