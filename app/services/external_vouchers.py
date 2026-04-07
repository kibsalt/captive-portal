"""External MySQL voucher database integration.

The external RADIUS/billing system stores M-Pesa payments in a MySQL `vouchers`
table where the phone number is SHA256-hashed for privacy.

Table schema (external, read-only from portal's perspective):
    vouchers:
        id          INT AUTO_INCREMENT PRIMARY KEY
        voucher     VARCHAR(64)     -- M-Pesa receipt code / RADIUS password
        mpesa_number VARCHAR(128)   -- SHA256 hash of phone (254XXXXXXXXX format)
        upspeed     INT             -- Upload speed in kbps
        downspeed   INT             -- Download speed in kbps
        downlimit   BIGINT          -- Data limit in bytes (0 = unlimited)
        amount      INT             -- Amount paid in KES
        session_end BIGINT          -- Unix timestamp when session ends (0 = unused)
        start_date  BIGINT          -- Unix timestamp when session started
        state       INT             -- 0 = available, 1 = expired/used

Hashing logic (matches the bash script):
    mpesa_hash = SHA256( phone.replace(/^0/, '254') )

    The stored hash may contain asterisks (*) as wildcards, so lookup uses:
    WHERE '{hash}' LIKE REPLACE(REPLACE(mpesa_number,' ',''),'*','%')

Query for valid voucher:
    - session_end=0 AND state=0  (unused, ready to activate)
    - session_end - 180 > UNIX_TIMESTAMP() AND state=0  (active with 3-min buffer)
"""

import hashlib
import logging

import pymysql

logger = logging.getLogger(__name__)


def hash_phone(phone: str) -> str:
    """Hash phone number the same way as the external billing system.

    Replicates: echo -n "$mpesa_number" | sed 's/^0/254/' | sha256sum | awk '{print $1}'
    """
    phone = phone.strip().replace(' ', '')
    # Normalize to 254XXXXXXXXX format
    if phone.startswith('0'):
        phone = '254' + phone[1:]
    elif phone.startswith('+254'):
        phone = '254' + phone[4:]
    elif phone.startswith('+'):
        phone = phone[1:]
    # If already starts with 254, keep as-is

    return hashlib.sha256(phone.encode()).hexdigest()


def _get_connection(config):
    """Create a connection to the external MySQL voucher database."""
    return pymysql.connect(
        host=config.get('EXTERNAL_DB_HOST', '127.0.0.1'),
        port=int(config.get('EXTERNAL_DB_PORT', 3306)),
        user=config.get('EXTERNAL_DB_USER', 'radius'),
        password=config.get('EXTERNAL_DB_PASSWORD', ''),
        database=config.get('EXTERNAL_DB_NAME', 'radius'),
        charset='utf8mb4',
        connect_timeout=5,
        read_timeout=10,
        cursorclass=pymysql.cursors.DictCursor,
    )


def lookup_by_phone(phone: str, config: dict) -> dict | None:
    """Look up a valid voucher record by M-Pesa phone number.

    Hashes the phone and queries the external DB using the wildcard LIKE pattern
    to handle asterisks in the stored hash.

    Args:
        phone: M-Pesa phone number (any format: 07xx, +254xx, 254xx)
        config: Flask app config dict

    Returns:
        dict with voucher details, or None if not found:
        {
            'voucher': str,       # M-Pesa receipt / RADIUS password
            'upspeed': int,       # Upload speed kbps
            'downspeed': int,     # Download speed kbps
            'downlimit': int,     # Data limit bytes
            'amount': int,        # Amount paid KES
            'session_end': int,   # Unix timestamp (0 = unused)
            'start_date': int,    # Unix timestamp
            'is_unused': bool,    # True if session_end == 0
        }
    """
    mpesa_hash = hash_phone(phone)

    try:
        conn = _get_connection(config)
        with conn.cursor() as cursor:
            # Exact replication of the bash query logic:
            # The stored mpesa_number may have * wildcards and spaces.
            # We replace spaces with nothing and * with % for LIKE matching.
            # We check OUR hash against THEIR pattern (not the other way around).
            query = """
                SELECT voucher, upspeed, downspeed, downlimit, amount, session_end, start_date
                FROM vouchers
                WHERE (
                    %s LIKE REPLACE(REPLACE(mpesa_number, ' ', ''), '*', '%%')
                    AND session_end = 0
                    AND state = 0
                ) OR (
                    %s LIKE REPLACE(REPLACE(mpesa_number, ' ', ''), '*', '%%')
                    AND session_end - 180 > UNIX_TIMESTAMP()
                    AND state = 0
                )
                ORDER BY id DESC
                LIMIT 1
            """
            cursor.execute(query, (mpesa_hash, mpesa_hash))
            row = cursor.fetchone()

        conn.close()

        if not row:
            logger.info(f'External voucher lookup: no record for phone hash {mpesa_hash[:16]}...')
            return None

        result = {
            'voucher': row['voucher'],
            'upspeed': int(row.get('upspeed', 0) or 0),
            'downspeed': int(row.get('downspeed', 0) or 0),
            'downlimit': int(row.get('downlimit', 0) or 0),
            'amount': int(row.get('amount', 0) or 0),
            'session_end': int(row.get('session_end', 0) or 0),
            'start_date': int(row.get('start_date', 0) or 0),
            'is_unused': int(row.get('session_end', 0) or 0) == 0,
        }

        logger.info(f'External voucher found: voucher={result["voucher"]} '
                    f'amount={result["amount"]} session_end={result["session_end"]}')
        return result

    except Exception as e:
        logger.error(f'External voucher lookup error: {e}')
        return None


def lookup_by_mpesa_code(mpesa_code: str, config: dict) -> dict | None:
    """Look up a voucher record by M-Pesa receipt code (the `voucher` column).

    Used when a user enters their M-Pesa confirmation code directly.

    Args:
        mpesa_code: M-Pesa receipt number (e.g. SJ12ABC345)
        config: Flask app config dict

    Returns:
        dict with voucher details, or None if not found
    """
    mpesa_code = mpesa_code.strip().upper()

    try:
        conn = _get_connection(config)
        with conn.cursor() as cursor:
            query = """
                SELECT voucher, upspeed, downspeed, downlimit, amount, session_end, start_date
                FROM vouchers
                WHERE voucher = %s
                AND state = 0
                AND (session_end = 0 OR session_end - 180 > UNIX_TIMESTAMP())
                ORDER BY id DESC
                LIMIT 1
            """
            cursor.execute(query, (mpesa_code,))
            row = cursor.fetchone()

        conn.close()

        if not row:
            logger.info(f'External voucher lookup by code: no record for {mpesa_code}')
            return None

        result = {
            'voucher': row['voucher'],
            'upspeed': int(row.get('upspeed', 0) or 0),
            'downspeed': int(row.get('downspeed', 0) or 0),
            'downlimit': int(row.get('downlimit', 0) or 0),
            'amount': int(row.get('amount', 0) or 0),
            'session_end': int(row.get('session_end', 0) or 0),
            'start_date': int(row.get('start_date', 0) or 0),
            'is_unused': int(row.get('session_end', 0) or 0) == 0,
        }

        logger.info(f'External voucher found by code: voucher={result["voucher"]} '
                    f'amount={result["amount"]}')
        return result

    except Exception as e:
        logger.error(f'External voucher lookup by code error: {e}')
        return None


def mark_voucher_used(mpesa_code: str, session_end_ts: int, config: dict) -> bool:
    """Mark an external voucher as used by setting session_end timestamp.

    Called after activating a session so the voucher can't be reused.

    Args:
        mpesa_code: The voucher code (M-Pesa receipt)
        session_end_ts: Unix timestamp when the session should end
        config: Flask app config dict
    """
    try:
        conn = _get_connection(config)
        with conn.cursor() as cursor:
            query = """
                UPDATE vouchers
                SET session_end = %s, start_date = UNIX_TIMESTAMP()
                WHERE voucher = %s AND session_end = 0 AND state = 0
                LIMIT 1
            """
            cursor.execute(query, (session_end_ts, mpesa_code))
            affected = cursor.rowcount

        conn.commit()
        conn.close()

        if affected:
            logger.info(f'External voucher marked used: {mpesa_code} → session_end={session_end_ts}')
        return affected > 0

    except Exception as e:
        logger.error(f'External voucher mark_used error: {e}')
        return False
