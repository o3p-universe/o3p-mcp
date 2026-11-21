import fnmatch
import os
import re
from urllib.parse import urlsplit, urlunsplit

import requests
from markupsafe import Markup

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.tools import config

from .api import DEFAULT_LOG_DAYS, DEFAULT_MAX_RESPONSE_BYTES, DEFAULT_TIMEOUT_SECONDS
from .denylist import DEFAULT_DENIED_FIELDS, DEFAULT_DENIED_MODELS, patterns
from .mcp_client import DEFAULT_REGISTRATION_LIMIT, MAX_REGISTRATION_LIMIT
from .api_sql import (
    DEFAULT_SQL_DATABASE,
    DEFAULT_SQL_MAX_COPY_BYTES,
    DEFAULT_SQL_MAX_ROWS,
    DEFAULT_SQL_PORT,
    DEFAULT_SQL_ROLES,
    DEFAULT_SQL_SSLMODE,
    DEFAULT_SQL_USER,
    DEFAULT_SQL_USER_CONCURRENCY,
    MAX_USER_SLOTS,
    PASSWORD_CONFIG_KEY,
    PASSWORD_ENV,
    SSL_AUTO,
    SSL_MODES,
)
from .oauth import (
    DEFAULT_ACCESS_TOKEN_MINUTES,
    DEFAULT_REFRESH_TOKEN_DAYS,
    INTROSPECT_PATH,
    METADATA_PATH,
    is_valid_issuer,
)


HEALTH_TIMEOUT = 10
# The most models the summary of the writable models names per pattern.
SUMMARY_MODELS = 5
SQL_NAME_RE = re.compile(r"[a-z_][a-z0-9_]{0,40}")
SQL_HOST_RE = re.compile(r"[A-Za-z0-9._:\[\]/-]{1,253}")
MAX_ACCESS_TOKEN_MINUTES = 24 * 60
MAX_REFRESH_TOKEN_DAYS = 365


def health_url(resource_url):
    """Return the ``/health`` URL next to the MCP endpoint.

    ``https://mcp.example.com/mcp`` gives ``https://mcp.example.com/health``.
    """
    parts = urlsplit(resource_url.rstrip("/"))
    path = parts.path.rsplit("/", 1)[0] + "/health"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def is_valid_resource_url(value):
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    return (
        parts.scheme in ("http", "https")
        and bool(parts.hostname)
        and not parts.query
        and not parts.fragment
        and parts.username is None
    )


def notification(title, message, kind, sticky=False):
    return {
        "type": "ir.actions.client",
        "tag": "display_notification",
        "params": {
            "title": title,
            "message": message,
            "type": kind,
            "sticky": sticky,
        },
    }


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    o3p_mcp_resource_url = fields.Char(
        string="MCP server URL",
        help="Public URL of the MCP endpoint; tokens are issued for this audience.",
    )
    o3p_mcp_access_token_minutes = fields.Integer(
        string="Access token lifetime (minutes)",
        default=DEFAULT_ACCESS_TOKEN_MINUTES,
    )
    o3p_mcp_registration_limit = fields.Integer(
        string="Client registrations per hour",
        default=DEFAULT_REGISTRATION_LIMIT,
        help="Clients that may register per hour from one address (IPv6: one /64 "
        "network). Hosted connectors register all their users from a few shared "
        "addresses.",
    )
    o3p_mcp_refresh_token_days = fields.Integer(
        string="Refresh token lifetime (days)",
        default=DEFAULT_REFRESH_TOKEN_DAYS,
    )
    o3p_mcp_issuer = fields.Char(
        string="Issuer",
        help="Public URL of this Odoo as the authorization server, such as "
        "https://odoo.example.com. Metadata, endpoints and iss use it.",
    )
    o3p_mcp_api_denied_models = fields.Text(
        string="Hidden models",
        help="Models the data API never exposes, one pattern per line (* matches "
        "anything). Applies to every user, administrators included.",
    )
    o3p_mcp_api_denied_fields = fields.Text(
        string="Hidden fields",
        help="Field names the data API never exposes on any model, one pattern "
        "per line. Binary fields are always hidden.",
    )
    o3p_mcp_api_writable_models = fields.Text(
        string="Writable models",
        help="Models whose records MCP record editors may create and change, one "
        "pattern per line (* matches anything). Empty: nothing can be written.",
    )
    # A helper of the form, never stored: the model picked is added to the
    # list above as its technical name.
    o3p_mcp_api_writable_add_id = fields.Many2one(
        "ir.model",
        string="Add a model",
        domain=lambda self: self._o3p_mcp_api_writable_candidates(),
        help="Search a model by its name to add it to the writable models.",
    )
    o3p_mcp_api_writable_summary = fields.Html(
        string="What the list opens",
        compute="_compute_o3p_mcp_api_writable_summary",
    )
    o3p_mcp_action_count = fields.Integer(
        string="Exposed actions",
        compute="_compute_o3p_mcp_action_count",
    )
    o3p_mcp_api_max_response_kb = fields.Integer(
        string="Largest answer (KB)",
        default=DEFAULT_MAX_RESPONSE_BYTES // 1000,
    )
    o3p_mcp_api_timeout_seconds = fields.Integer(
        string="Query time limit (seconds)",
        default=DEFAULT_TIMEOUT_SECONDS,
    )
    o3p_mcp_api_log_days = fields.Integer(
        string="Keep call log (days)",
        default=DEFAULT_LOG_DAYS,
    )
    o3p_mcp_sql_host = fields.Char(
        string="Sandbox host",
        help="Host name of the separate PostgreSQL instance that runs SQL queries. Empty disables SQL.",
    )
    o3p_mcp_sql_port = fields.Integer(string="Sandbox port", default=DEFAULT_SQL_PORT)
    o3p_mcp_sql_database = fields.Char(string="Sandbox database", default=DEFAULT_SQL_DATABASE)
    o3p_mcp_sql_user = fields.Char(
        string="Sandbox roles",
        default=DEFAULT_SQL_USER,
        help="Role name prefix: the roles are <prefix>_1 … <prefix>_<count>.",
    )
    o3p_mcp_sql_roles = fields.Integer(
        string="Sandbox role count",
        default=DEFAULT_SQL_ROLES,
        help="How many SQL queries may run at once, over all users.",
    )
    o3p_mcp_sql_sslmode = fields.Selection(
        [(SSL_AUTO, "automatic: prefer on this host, verify-full elsewhere")]
        + [(mode, mode) for mode in SSL_MODES],
        string="Sandbox SSL mode",
        default=DEFAULT_SQL_SSLMODE,
        help="libpq sslmode of the sandbox connections. Automatic uses prefer for a "
        "sandbox on this host (unix socket, localhost, loopback address, or a "
        "single-label Docker service name) and verify-full anywhere else.",
    )
    o3p_mcp_sql_sslrootcert = fields.Char(
        string="Sandbox CA certificates",
        help="libpq sslrootcert for verify-ca and verify-full: a file of CA "
        "certificates, or 'system' for the system's CA store. Empty: libpq's default.",
    )
    o3p_mcp_sql_user_concurrency = fields.Integer(
        string="SQL queries per user", default=DEFAULT_SQL_USER_CONCURRENCY
    )
    o3p_mcp_sql_max_rows = fields.Integer(
        string="Rows per table",
        default=DEFAULT_SQL_MAX_ROWS,
        help="Largest number of rows of one table a query may read.",
    )
    o3p_mcp_sql_max_copy_mb = fields.Integer(
        string="Data per query (MB)",
        default=DEFAULT_SQL_MAX_COPY_BYTES // 1_000_000,
        help="Largest amount of table data copied into the sandbox for one query.",
    )
    o3p_mcp_sql_password_source = fields.Char(
        string="Sandbox password", compute="_compute_o3p_mcp_sql_password_source"
    )
    o3p_mcp_metadata_url = fields.Char(
        string="Metadata URL",
        compute="_compute_o3p_mcp_urls",
    )
    o3p_mcp_introspection_url = fields.Char(
        string="Introspection URL",
        compute="_compute_o3p_mcp_urls",
    )
    o3p_mcp_resource_server_client_id = fields.Char(
        string="Introspection client ID",
        compute="_compute_o3p_mcp_urls",
    )

    @api.depends_context("uid")
    def _compute_o3p_mcp_urls(self):
        issuer = self.env["o3p.mcp.oauth"]._issuer()
        server = self.env["o3p.mcp.client"].sudo()._resource_server()
        for settings in self:
            settings.o3p_mcp_metadata_url = f"{issuer}{METADATA_PATH}"
            settings.o3p_mcp_introspection_url = f"{issuer}{INTROSPECT_PATH}"
            settings.o3p_mcp_resource_server_client_id = server.client_id

    def _compute_o3p_mcp_sql_password_source(self):
        if os.environ.get(PASSWORD_ENV):
            source = self.env._("Set by the %(name)s environment variable", name=PASSWORD_ENV)
        elif config.get(PASSWORD_CONFIG_KEY):
            source = self.env._("Set by %(key)s in the Odoo configuration file", key=PASSWORD_CONFIG_KEY)
        else:
            source = self.env._(
                "Missing: set the %(name)s environment variable or %(key)s in the Odoo configuration file",
                name=PASSWORD_ENV,
                key=PASSWORD_CONFIG_KEY,
            )
        for settings in self:
            settings.o3p_mcp_sql_password_source = source

    @api.model
    def _o3p_mcp_api_writable_candidates(self):
        """The models that can be opened for writing, as a domain on ir.model."""
        data_api = self.env["o3p.mcp.api"].sudo()
        names = [name for name in self.env.registry if data_api._model_can_be_opened(name)]
        return [("model", "in", sorted(names))]

    @api.onchange("o3p_mcp_api_writable_add_id")
    def _onchange_o3p_mcp_api_writable_add_id(self):
        name = self.o3p_mcp_api_writable_add_id.model
        if not name:
            return
        self.o3p_mcp_api_writable_add_id = False
        listed = patterns(self.o3p_mcp_api_writable_models, ())
        if not any(fnmatch.fnmatchcase(name, item) for item in listed):
            self.o3p_mcp_api_writable_models = "\n".join([*listed, name])

    def _compute_o3p_mcp_action_count(self):
        self.o3p_mcp_action_count = self.env["o3p.mcp.action"].sudo().search_count([])

    @api.depends("o3p_mcp_api_writable_models")
    def _compute_o3p_mcp_api_writable_summary(self):
        """Say in words what each line of the writable models opens."""
        data_api = self.env["o3p.mcp.api"].sudo()
        labels = {
            row["model"]: row["name"]
            for row in self.env["ir.model"].sudo().search_read([], ["model", "name"])
        }
        for settings in self:
            items = []
            for pattern in patterns(settings.o3p_mcp_api_writable_models, ()):
                matched = sorted(name for name in labels if fnmatch.fnmatchcase(name, pattern))
                opened = [name for name in matched if data_api._model_can_be_opened(name)]
                if not matched:
                    text = self.env._("no model has this name")
                elif not opened:
                    text = self.env._("not writable through MCP")
                elif opened == [pattern]:
                    text = labels[pattern]
                else:
                    text = ", ".join(
                        f"{labels[name]} ({name})" for name in opened[:SUMMARY_MODELS]
                    )
                    if len(opened) > SUMMARY_MODELS:
                        text = self.env._(
                            "%(models)s and %(count)s more",
                            models=text,
                            count=len(opened) - SUMMARY_MODELS,
                        )
                    if len(opened) < len(matched):
                        text = self.env._(
                            "%(models)s; %(count)s left out, not writable through MCP",
                            models=text,
                            count=len(matched) - len(opened),
                        )
                items.append(Markup("<li><code>%s</code>: %s</li>") % (pattern, text))
            settings.o3p_mcp_api_writable_summary = (
                Markup("<ul>%s</ul>") % Markup().join(items)
                if items
                else Markup("<p>%s</p>") % self.env._("Nothing can be written.")
            )

    @api.model
    def get_values(self):
        values = super().get_values()
        params = self.env["ir.config_parameter"].sudo()
        values.update(
            o3p_mcp_issuer=self.env["o3p.mcp.oauth"]._issuer(),
            o3p_mcp_resource_url=params.get_str("o3p_mcp.resource_url", ""),
            o3p_mcp_access_token_minutes=params.get_int(
                "o3p_mcp.access_token_minutes", DEFAULT_ACCESS_TOKEN_MINUTES
            ),
            o3p_mcp_registration_limit=params.get_int(
                "o3p_mcp.registration_limit", DEFAULT_REGISTRATION_LIMIT
            ),
            o3p_mcp_refresh_token_days=params.get_int(
                "o3p_mcp.refresh_token_days", DEFAULT_REFRESH_TOKEN_DAYS
            ),
            o3p_mcp_api_denied_models="\n".join(
                patterns(params.get_str("o3p_mcp.api_denied_models"), DEFAULT_DENIED_MODELS)
            ),
            o3p_mcp_api_denied_fields="\n".join(
                patterns(params.get_str("o3p_mcp.api_denied_fields"), DEFAULT_DENIED_FIELDS)
            ),
            o3p_mcp_api_writable_models="\n".join(
                patterns(params.get_str("o3p_mcp.api_writable_models"), ())
            ),
            o3p_mcp_api_max_response_kb=params.get_int(
                "o3p_mcp.api_max_response_bytes", DEFAULT_MAX_RESPONSE_BYTES
            )
            // 1000,
            o3p_mcp_api_timeout_seconds=params.get_int(
                "o3p_mcp.api_timeout_seconds", DEFAULT_TIMEOUT_SECONDS
            ),
            o3p_mcp_api_log_days=params.get_int("o3p_mcp.api_log_days", DEFAULT_LOG_DAYS),
        )
        sql = self.env["o3p.mcp.api"]._sql_settings()
        values.update(
            o3p_mcp_sql_host=sql["host"],
            o3p_mcp_sql_port=sql["port"],
            o3p_mcp_sql_database=sql["database"],
            o3p_mcp_sql_user=sql["user"],
            o3p_mcp_sql_roles=sql["roles"],
            o3p_mcp_sql_sslmode=sql["sslmode_setting"],
            o3p_mcp_sql_sslrootcert=sql["sslrootcert"],
            o3p_mcp_sql_user_concurrency=sql["user_concurrency"],
            o3p_mcp_sql_max_rows=sql["max_rows"],
            o3p_mcp_sql_max_copy_mb=sql["max_copy_bytes"] // 1_000_000,
        )
        return values

    def set_values(self):
        issuer = (self.o3p_mcp_issuer or "").strip().rstrip("/")
        # Checked only when changed, so that an issuer inherited from
        # web.base.url at install does not block unrelated settings.
        if issuer != self.env["o3p.mcp.oauth"]._issuer() and not is_valid_issuer(issuer):
            raise UserError(
                self.env._(
                    "The issuer must be an https URL without path, such as "
                    "https://odoo.example.com (http only on localhost)."
                )
            )
        resource_url = (self.o3p_mcp_resource_url or "").strip()
        if resource_url and not is_valid_resource_url(resource_url):
            raise UserError(
                self.env._(
                    "The MCP server URL must be an http or https URL without query "
                    "or fragment, such as https://mcp.example.com/mcp."
                )
            )
        if not 1 <= self.o3p_mcp_access_token_minutes <= MAX_ACCESS_TOKEN_MINUTES:
            raise UserError(
                self.env._(
                    "The access token lifetime must be between 1 and %(max)s minutes.",
                    max=MAX_ACCESS_TOKEN_MINUTES,
                )
            )
        if not 1 <= self.o3p_mcp_registration_limit <= MAX_REGISTRATION_LIMIT:
            raise UserError(
                self.env._(
                    "The client registrations per hour must be between 1 and %(max)s.",
                    max=MAX_REGISTRATION_LIMIT,
                )
            )
        if not 1 <= self.o3p_mcp_refresh_token_days <= MAX_REFRESH_TOKEN_DAYS:
            raise UserError(
                self.env._(
                    "The refresh token lifetime must be between 1 and %(max)s days.",
                    max=MAX_REFRESH_TOKEN_DAYS,
                )
            )
        if not 1 <= self.o3p_mcp_api_max_response_kb <= 100_000:
            raise UserError(self.env._("The largest answer must be between 1 and 100000 KB."))
        if not 1 <= self.o3p_mcp_api_timeout_seconds <= 300:
            raise UserError(
                self.env._("The query time limit must be between 1 and 300 seconds.")
            )
        if not 1 <= self.o3p_mcp_api_log_days <= 3650:
            raise UserError(self.env._("The call log must be kept between 1 and 3650 days."))
        self._check_o3p_mcp_sql_values()
        result = super().set_values()
        params = self.env["ir.config_parameter"].sudo()
        # The default lists are not stored, so that a later version's
        # additions to them apply.
        for key, value, default in (
            ("o3p_mcp.api_denied_models", self.o3p_mcp_api_denied_models, DEFAULT_DENIED_MODELS),
            ("o3p_mcp.api_denied_fields", self.o3p_mcp_api_denied_fields, DEFAULT_DENIED_FIELDS),
        ):
            items = patterns(value, default)
            params.set_str(key, "" if items == tuple(default) else "\n".join(items))
        params.set_str(
            "o3p_mcp.api_writable_models",
            "\n".join(patterns(self.o3p_mcp_api_writable_models, ())),
        )
        params.set_int("o3p_mcp.api_max_response_bytes", self.o3p_mcp_api_max_response_kb * 1000)
        params.set_int("o3p_mcp.api_timeout_seconds", self.o3p_mcp_api_timeout_seconds)
        params.set_int("o3p_mcp.api_log_days", self.o3p_mcp_api_log_days)
        params.set_str("o3p_mcp.issuer", issuer)
        params.set_str("o3p_mcp.resource_url", resource_url)
        params.set_int("o3p_mcp.access_token_minutes", self.o3p_mcp_access_token_minutes)
        params.set_int("o3p_mcp.refresh_token_days", self.o3p_mcp_refresh_token_days)
        params.set_int("o3p_mcp.registration_limit", self.o3p_mcp_registration_limit)
        params.set_str("o3p_mcp.sql_host", (self.o3p_mcp_sql_host or "").strip())
        params.set_int("o3p_mcp.sql_port", self.o3p_mcp_sql_port)
        params.set_str("o3p_mcp.sql_database", self.o3p_mcp_sql_database.strip())
        params.set_str("o3p_mcp.sql_user", self.o3p_mcp_sql_user.strip())
        params.set_int("o3p_mcp.sql_roles", self.o3p_mcp_sql_roles)
        params.set_str("o3p_mcp.sql_sslmode", self.o3p_mcp_sql_sslmode or DEFAULT_SQL_SSLMODE)
        params.set_str("o3p_mcp.sql_sslrootcert", (self.o3p_mcp_sql_sslrootcert or "").strip())
        params.set_int("o3p_mcp.sql_user_concurrency", self.o3p_mcp_sql_user_concurrency)
        params.set_int("o3p_mcp.sql_max_rows", self.o3p_mcp_sql_max_rows)
        params.set_int("o3p_mcp.sql_max_copy_bytes", self.o3p_mcp_sql_max_copy_mb * 1_000_000)
        return result

    def _check_o3p_mcp_sql_values(self):
        _ = self.env._
        host = (self.o3p_mcp_sql_host or "").strip()
        if host and not SQL_HOST_RE.fullmatch(host):
            raise UserError(_("The sandbox host must be a host name or an IP address."))
        if not 1 <= self.o3p_mcp_sql_port <= 65535:
            raise UserError(_("The sandbox port must be between 1 and 65535."))
        for value, label in (
            (self.o3p_mcp_sql_database, _("sandbox database")),
            (self.o3p_mcp_sql_user, _("sandbox role prefix")),
        ):
            if not SQL_NAME_RE.fullmatch((value or "").strip()):
                raise UserError(
                    _("The %(label)s must be a lowercase PostgreSQL name (letters, digits, _).", label=label)
                )
        rootcert = (self.o3p_mcp_sql_sslrootcert or "").strip()
        if len(rootcert) > 4096 or any(char in rootcert for char in "\r\n\0"):
            raise UserError(_("The sandbox CA certificates must be a file path or 'system'."))
        if not 1 <= self.o3p_mcp_sql_roles <= 100:
            raise UserError(_("The sandbox role count must be between 1 and 100."))
        if not 1 <= self.o3p_mcp_sql_user_concurrency <= MAX_USER_SLOTS:
            raise UserError(
                _("SQL queries per user must be between 1 and %(max)s.", max=MAX_USER_SLOTS)
            )
        if not 1 <= self.o3p_mcp_sql_max_rows <= 100_000_000:
            raise UserError(_("Rows per table must be between 1 and 100000000."))
        if not 1 <= self.o3p_mcp_sql_max_copy_mb <= 10_000:
            raise UserError(_("Data per query must be between 1 and 10000 MB."))

    def action_o3p_mcp_check_sql(self):
        """Connect as every sandbox role and report what it may do."""
        self.ensure_one()
        self.env["o3p.mcp.client"].check_access("write")
        level, lines = self.env["o3p.mcp.api"].sudo()._sql_check()
        return notification(
            self.env._("SQL sandbox"),
            "\n".join(lines),
            level,
            sticky=level != "success",
        )

    def action_o3p_mcp_rotate_credentials(self):
        """Create or rotate the introspection credentials; show the secret once."""
        self.ensure_one()
        self.env["o3p.mcp.client"].check_access("write")
        server, secret = self.env["o3p.mcp.client"]._rotate_resource_server_secret()
        _ = self.env._
        return notification(
            _("Introspection credentials"),
            _(
                "Client ID: %(client_id)s — Client secret: %(secret)s — Copy the "
                "secret now: it is not shown again and the previous one no longer "
                "works.",
                client_id=server.client_id,
                secret=secret,
            ),
            "warning",
            sticky=True,
        )

    def action_o3p_mcp_check_health(self):
        """Call the MCP server's health endpoint and report the outcome."""
        self.ensure_one()
        self.env["o3p.mcp.client"].check_access("read")
        _ = self.env._
        resource_url = (
            self.o3p_mcp_resource_url or self.env["o3p.mcp.oauth"]._resource_url()
        ).strip()
        if not is_valid_resource_url(resource_url):
            raise UserError(_("Set a valid MCP server URL first."))
        url = health_url(resource_url)
        try:
            response = requests.get(url, timeout=HEALTH_TIMEOUT)
        except requests.RequestException as error:
            return notification(
                _("MCP server"),
                _(
                    "%(url)s could not be reached (%(error)s).",
                    url=url,
                    error=type(error).__name__,
                ),
                "danger",
            )
        if response.ok:
            return notification(
                _("MCP server"),
                _("%(url)s answered HTTP %(status)s.", url=url, status=response.status_code),
                "success",
            )
        return notification(
            _("MCP server"),
            _("%(url)s answered HTTP %(status)s.", url=url, status=response.status_code),
            "warning",
        )
