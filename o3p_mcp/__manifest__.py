{
    "name": "o3p - mcp",
    "summary": "OAuth 2.1 sign-in and a read-only data API for an external MCP server.",
    "description": (
        "O3P MCP lets Odoo users sign in to an external MCP server: Odoo issues "
        "and introspects OAuth 2.1 access tokens whose scopes follow the user's "
        "MCP groups. The MCP server reads Odoo records, and runs read-only SQL "
        "in a separate PostgreSQL sandbox, with the rights of the signed-in user."
    ),
    "version": "18.0.1.43.0",
    "category": "Technical",
    "author": "O3P",
    "website": "https://github.com/o3p-universe/o3p-mcp",
    "license": "LGPL-3",
    "depends": ["base_setup", "web"],
    "external_dependencies": {
        "python": ["idna", "pglast", "requests"],
    },
    "data": [
        "security/o3p_mcp_security.xml",
        "security/ir.model.access.csv",
        "data/ir_config_parameter_data.xml",
        "data/ir_cron_data.xml",
        "views/oauth_templates.xml",
        "views/mcp_client_views.xml",
        "views/mcp_token_views.xml",
        "views/mcp_api_log_views.xml",
        "views/res_config_settings_views.xml",
    ],
    "auto_install": False,
    "installable": True,
    "application": False,
}
