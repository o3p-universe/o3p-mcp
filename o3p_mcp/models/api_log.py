from odoo import fields, models


class O3pMcpApiLog(models.Model):
    _name = "o3p.mcp.api.log"
    _description = "MCP Data API Call"
    _order = "id desc"

    # Who called what, how it ended and how long it took. Parameters and
    # results are never stored.
    user_id = fields.Many2one("res.users", readonly=True, index=True, ondelete="set null")
    client_id = fields.Many2one(
        "o3p.mcp.client", string="MCP client", readonly=True, ondelete="set null"
    )
    op = fields.Char(string="Operation", readonly=True)
    model = fields.Char(readonly=True)
    tables = fields.Char(readonly=True, help="Tables an SQL query read, comma-separated.")
    status = fields.Integer(string="HTTP status", readonly=True)
    error = fields.Char(string="Error code", readonly=True)
    duration_ms = fields.Integer(string="Duration (ms)", readonly=True)
    row_count = fields.Integer(string="Rows", readonly=True)
