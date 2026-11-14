"""What MCP users never get from Odoo, whatever their rights.

One place for every way o3p_mcp exposes Odoo data (the data API and its SQL
operation): the default lists, the setting format and the matching.
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
    "snailmail.letter",
    "bus.*",
)
# Models the SQL operation never copies, on top of the lists above: their
# read security lives in Python code (_search post-filtering, read checks)
# that the copy of _search's rows would not reproduce faithfully. Models
# whose class overrides how records are read are left out automatically.
SQL_DENIED_MODELS = (
    "ir.attachment",
    "mail.message",
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


# Methods that change what a read returns after the search. What a model
# hides in them (calendar.event blanks private events) is still matched by
# its domains, sorted by its order and counted by its groups, so such a model
# is only filtered, sorted and grouped on ``id`` and on the fields it declares
# public (``_get_public_fields``), unless it is reviewed below.
READ_OVERRIDES = ("read", "_read", "fetch", "_fetch_query", "search_fetch", "_read_format")
ORM_MODULE_PREFIX = "odoo.orm."
# Models whose read override does not hide anything from the user (model:
# what the override does), reviewed on a standard Odoo 18 and 20 install.
READ_OVERRIDES_REVIEWED = {
    "account.move": "read() only supplies the default fields",
    "account.move.line": "read() supplies the default fields; search_fetch() computes the cumulated balance",
    "crm.lead": "search_fetch() supports ordering on the user's next activity deadline",
    "hr.employee": "delegates to hr.employee.public only for users who may not read hr.employee, "
    "whom the data API refuses already",
    "ir.actions.act_window": "read() fills in the empty-list help text",
    "mrp.bom.line": "search_fetch() keeps exploded lines in hierarchy order",
}


def overrides_read(model_class):
    """Tell whether an addon overrides how ``model_class`` reads records."""
    for method in READ_OVERRIDES:
        owner = next((klass for klass in model_class.__mro__ if method in vars(klass)), None)
        if owner is not None and not owner.__module__.startswith(ORM_MODULE_PREFIX):
            return True
    return False
