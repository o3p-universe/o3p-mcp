"""What MCP users never get from Odoo, whatever their rights.

One place for every way o3p_mcp exposes Odoo data (the data API, and later
the SQL operation): the default lists, the setting format and the matching.
``o3p.mcp.api._model_denied`` / ``_field_denied`` apply the configured lists.
"""

import fnmatch


# Models holding credentials or payment references, and models that index
# other models' records (by model name and id) without checking that the user
# may read those records.
DEFAULT_DENIED_MODELS = (
    "ir.config_parameter",
    "res.users.apikeys*",
    "o3p.mcp.*",
    "auth_totp*",
    "ir.mail_server",
    "fetchmail.server",
    "payment.provider",
    "payment.token",
    "payment.transaction",
    "mail.followers",
    "mail.notification",
    "mail.tracking.value",
    "mail.push.device*",
    "rating.rating",
    "bus.*",
)
# Field names hidden on every model. Binary fields, and relational fields to
# a hidden model, are hidden too.
DEFAULT_DENIED_FIELDS = (
    "*password*",
    "*secret*",
    "*token*",
    "*api_key*",
    "*shared_key*",
    "provider_ref*",
)


def patterns(value, default):
    """Return the fnmatch patterns of a setting: one per line or comma.

    An empty setting means the default list.
    """
    items = [item.strip() for item in (value or "").replace(",", "\n").splitlines()]
    return tuple(item for item in items if item) or tuple(default)


def model_denied(name, model_patterns):
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in model_patterns)


def field_denied(field, field_patterns, model_patterns):
    name = field.name.lower()
    if field.type == "binary" or any(
        fnmatch.fnmatchcase(name, pattern.lower()) for pattern in field_patterns
    ):
        return True
    return bool(field.relational and model_denied(field.comodel_name, model_patterns))
