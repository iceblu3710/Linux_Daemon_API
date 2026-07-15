class AdminError(RuntimeError):
    code = "admin_error"


class AuthorizationError(AdminError):
    code = "not_authorized"


class ValidationError(AdminError):
    code = "invalid_request"


class NotFoundError(AdminError):
    code = "not_found"


class BusyError(AdminError):
    code = "busy"
