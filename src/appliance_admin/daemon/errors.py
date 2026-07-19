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


class NetworkBackendUnavailableError(AdminError):
    code = "network_backend_unavailable"


class NetworkActivationError(AdminError):
    code = "network_activation_failed"
