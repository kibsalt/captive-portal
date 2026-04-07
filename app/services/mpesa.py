"""M-Pesa payment via Lexabensa gateway.

STK Push:  POST https://lexabensa.com/paying/payment.php  (amount + payer)
Verify:    GET  https://lexabensa.com/api/?code=<code>

The verify endpoint accepts BOTH:
  - Phone number:  ?code=0729597196  → returns voucher for that phone
  - M-Pesa code:   ?code=UD2GEB7XGS → returns voucher by receipt code

Response is CSV:
  voucher,upspeed,downspeed,downlimit,amount,session_end,start_date

  - voucher:     M-Pesa receipt number (e.g. UD2GEB7XGS)
  - upspeed:     Upload speed in kbps (e.g. 4096)
  - downspeed:   Download speed in kbps (e.g. 4096)
  - downlimit:   Data limit in bytes (0 = unlimited)
  - amount:      KES paid (e.g. 5, 10, 100)
  - session_end: Unix timestamp when session ends (0 = unused/available)
  - start_date:  Unix timestamp when session started (0 = not started)

No Daraja API keys needed — Lexabensa handles the M-Pesa integration.

Uses direct IP (13.247.238.26) with Host header to work on servers
without DNS resolution. SSL verify disabled since cert is for the domain.
"""

import logging

import requests
from urllib3.exceptions import InsecureRequestWarning

requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

logger = logging.getLogger(__name__)

# Direct IP to bypass DNS — Host header routes to the right vhost
LEXABENSA_STK_URL = 'https://13.247.238.26/paying/payment.php'
LEXABENSA_API_URL = 'https://13.247.238.26/api/'
LEXABENSA_HEADERS = {'Host': 'lexabensa.com'}


def normalize_phone(phone: str) -> str:
    """Normalize to 07XX format for Lexabensa API."""
    phone = phone.strip().replace(' ', '')
    if phone.startswith('+254'):
        phone = '0' + phone[4:]
    elif phone.startswith('254') and len(phone) > 10:
        phone = '0' + phone[3:]
    return phone


def stk_push(phone: str, amount: int) -> dict:
    """Trigger M-Pesa STK push via Lexabensa.

    Returns:
        {'success': True/False, 'message': str}
    """
    phone = normalize_phone(phone)
    try:
        response = requests.post(
            LEXABENSA_STK_URL,
            headers=LEXABENSA_HEADERS,
            data={'amount': int(amount), 'payer': phone},
            timeout=30,
            verify=False,
        )
        logger.info(f'STK push sent: phone={phone} amount={amount} status={response.status_code}')
        return {
            'success': response.status_code == 200,
            'message': f'M-Pesa STK push sent to {phone}. Confirm on your phone.',
        }
    except requests.exceptions.RequestException as e:
        logger.error(f'STK push failed: {e}')
        return {'success': False, 'message': f'Payment service error: {e}'}


def _parse_lexabensa_response(raw: str) -> dict | None:
    """Parse Lexabensa CSV response into a structured dict.

    Format: voucher,upspeed,downspeed,downlimit,amount,session_end,start_date
    Example: UD2GEB7XGS,4096,4096,0,5,0,0
    May have parentheses: (UD2GEB7XGS,4096,4096,0,5,0,0)
    """
    if not raw:
        return None

    clean = raw.strip('() \n\r')
    parts = [p.strip() for p in clean.split(',')]

    if len(parts) < 5:
        return None

    voucher = parts[0]
    # If the voucher looks like just "0" or empty, it's not a real record
    if not voucher or voucher == '0':
        return None

    def safe_int(val, default=0):
        try:
            return int(val)
        except (ValueError, TypeError):
            return default

    return {
        'voucher': voucher,
        'upspeed': safe_int(parts[1]) if len(parts) > 1 else 0,
        'downspeed': safe_int(parts[2]) if len(parts) > 2 else 0,
        'downlimit': safe_int(parts[3]) if len(parts) > 3 else 0,
        'amount': safe_int(parts[4]) if len(parts) > 4 else 0,
        'session_end': safe_int(parts[5]) if len(parts) > 5 else 0,
        'start_date': safe_int(parts[6]) if len(parts) > 6 else 0,
    }


def _call_lexabensa(code: str) -> dict:
    """Call the Lexabensa API with a code (phone number or M-Pesa receipt).

    Returns:
        {
            'found': bool,
            'voucher': str,       # M-Pesa receipt code
            'upspeed': int,       # kbps
            'downspeed': int,     # kbps
            'downlimit': int,     # bytes (0 = unlimited)
            'amount': int,        # KES
            'session_end': int,   # unix timestamp (0 = unused)
            'start_date': int,    # unix timestamp (0 = not started)
            'is_unused': bool,    # session_end == 0
            'raw': str,
        }
    """
    try:
        response = requests.get(
            LEXABENSA_API_URL,
            headers=LEXABENSA_HEADERS,
            params={'code': code},
            timeout=15,
            verify=False,
        )
        raw = response.text.strip()
        logger.info(f'Lexabensa API: code={code} response={raw}')

        parsed = _parse_lexabensa_response(raw)
        if not parsed:
            return {'found': False, 'raw': raw}

        parsed['found'] = True
        parsed['is_unused'] = parsed['session_end'] == 0
        parsed['raw'] = raw
        # Alias for backwards compatibility
        parsed['mpesa_code'] = parsed['voucher']
        return parsed

    except requests.exceptions.RequestException as e:
        logger.error(f'Lexabensa API call failed for code={code}: {e}')
        return {'found': False, 'raw': str(e)}


def check_payment(phone: str) -> dict:
    """Check payment status by phone number.

    Calls: GET https://lexabensa.com/api/?code=0729597196
    Returns the full voucher record if found.
    """
    phone = normalize_phone(phone)
    return _call_lexabensa(phone)


def check_mpesa_code(mpesa_code: str) -> dict:
    """Check payment status by M-Pesa receipt code.

    Calls: GET https://lexabensa.com/api/?code=UD2GEB7XGS
    Returns the full voucher record if found.
    """
    mpesa_code = mpesa_code.strip().upper()
    return _call_lexabensa(mpesa_code)
