# Earlier versions stored the default denylists when the settings were saved,
# which would hide this version's additions to them. Forget stored values
# equal to the earlier defaults.
OLD_DEFAULTS = {
    "o3p_mcp.api_denied_models": "\n".join(
        [
            "ir.config_parameter",
            "res.users.apikeys*",
            "o3p.mcp.*",
            "auth_totp*",
            "ir.mail_server",
            "fetchmail.server",
            "payment.provider",
        ]
    ),
    "o3p_mcp.api_denied_fields": "\n".join(["*password*", "*secret*", "*token*", "*api_key*"]),
}


def migrate(cr, version):
    for key, value in OLD_DEFAULTS.items():
        cr.execute(
            "DELETE FROM ir_config_parameter WHERE key = %s AND value = %s", [key, value]
        )
