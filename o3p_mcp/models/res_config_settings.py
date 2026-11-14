from urllib.parse import urlsplit, urlunsplit

import requests

from odoo import api, fields, models
from odoo.exceptions import UserError

from .oauth import (
    DEFAULT_ACCESS_TOKEN_MINUTES,
    DEFAULT_REFRESH_TOKEN_DAYS,
    INTROSPECT_PATH,
    METADATA_PATH,
    is_valid_issuer,
)


HEALTH_TIMEOUT = 10
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
    o3p_mcp_refresh_token_days = fields.Integer(
        string="Refresh token lifetime (days)",
        default=DEFAULT_REFRESH_TOKEN_DAYS,
    )
    o3p_mcp_issuer = fields.Char(
        string="Issuer",
        help="Public URL of this Odoo as the authorization server, such as "
        "https://odoo.example.com. Metadata, endpoints and iss use it.",
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
            o3p_mcp_refresh_token_days=params.get_int(
                "o3p_mcp.refresh_token_days", DEFAULT_REFRESH_TOKEN_DAYS
            ),
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
        if not 1 <= self.o3p_mcp_refresh_token_days <= MAX_REFRESH_TOKEN_DAYS:
            raise UserError(
                self.env._(
                    "The refresh token lifetime must be between 1 and %(max)s days.",
                    max=MAX_REFRESH_TOKEN_DAYS,
                )
            )
        result = super().set_values()
        params = self.env["ir.config_parameter"].sudo()
        params.set_str("o3p_mcp.issuer", issuer)
        params.set_str("o3p_mcp.resource_url", resource_url)
        params.set_int("o3p_mcp.access_token_minutes", self.o3p_mcp_access_token_minutes)
        params.set_int("o3p_mcp.refresh_token_days", self.o3p_mcp_refresh_token_days)
        return result

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
