from frappe.model.document import Document


class MetaCAPIEvent(Document):
    """One delivered chat order and where it stands on its way to Meta.

    Rows are created by the hourly queue job and changed only through the
    approve / reject methods and the sender; nothing personal is stored here.
    """

    pass
