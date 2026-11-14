{
    "name": "o3p - mcp",
    "summary": "OAuth 2.1 authorization server for MCP clients.",
    "description": (
        "O3P MCP lets Odoo users sign in to an external MCP server: Odoo issues "
        "and introspects OAuth 2.1 access tokens whose scopes follow the user's "
        "MCP groups."
    ),
    "version": "20.0.1.3.0",
    "category": "Technical",
    "author": "O3P",
    "website": "https://github.com/o3p-universe/o3p-mcp",
    "license": "LGPL-3",
    "depends": ["base_setup", "web"],
    "data": [
        "security/o3p_mcp_security.xml",
        "security/ir.access.csv",
        "data/ir_cron_data.xml",
        "views/oauth_templates.xml",
    ],
    "auto_install": False,
    "installable": True,
    "application": False,
}
