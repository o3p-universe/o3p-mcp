def migrate(cr, version):
    # Earlier versions used web.base.url as the issuer: keep its current value.
    cr.execute(
        """
        INSERT INTO ir_config_parameter (key, value, create_date, write_date)
        SELECT 'o3p_mcp.issuer', rtrim(trim(value), '/'), now(), now()
          FROM ir_config_parameter
         WHERE key = 'web.base.url'
        ON CONFLICT (key) DO NOTHING
        """
    )
