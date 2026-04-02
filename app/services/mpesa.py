"""M-Pesa payment via Lexabensa gateway (direct IP, bypasses DNS).

STK Push:  POST https://13.247.238.26/paying/payment.php  (amount + payer)
Verify:    GET  https://13.247.238.26/api/?code=<phone>

Uses Host header to route through Nginx virtual hosting.
SSL verification disabled since cert is issued for lexabensa.com, not the IP.

Verification response is CSV:
  mpesa_code,field1,field2,field3,amount,field5,start_time

  - amount:     KES paid (e.g. 10, 5)
  - start_time: unix timestamp if session used, 0 if unused/available
  - mpesa_code: M-Pesa receipt number
"""

import logging

import requests
from urllib3.exceptions import InsecureRequestWarning

# Suppress SSL warnings since we hit the IP directly with verify=False
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

logger = logging.getLogger(__name__)

LEXABENSA_HOST = 'lexabensa.com'
LEXABENSA_STK_URL = 'https://13.247.238.26/paying/payment.php'
LEXABENSA_API_URL = 'https://13.247.238.26/api/'
LEXABENSA_HEADERS = {'Host': LEXABENSA_HOST}


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


def check_payment(phone: str) -> dict:
    """Check payment status via Lexabensa verification API.

    Returns:
        {
            'found': bool,
            'mpesa_code': str,
            'amount': int,
            'start_time': int,
            'is_unused': bool,    # start_time == 0 means available
            'raw': str,
        }
    """
    phone = normalize_phone(phone)
    try:
        response = requests.get(
            LEXABENSA_API_URL,
            headers=LEXABENSA_HEADERS,
            params={'code': phone},
            timeout=15,
            verify=False,
        )
        raw = response.text.strip()
        logger.info(f'Payment check: phone={phone} response={raw}')

        if not raw or raw.startswith('0') and ',' not in raw:
            return {'found': False, 'raw': raw}

        # Parse CSV: mpesa_code,f1,f2,f3,amount,f5,start_time
        # Handle parentheses: "(TFA1BM87H1,4096,4096,0,10,0,1754422908)"
        clean = raw.strip('() \n\r')
        parts = [p.strip() for p in clean.split(',')]

        if len(parts) < 5:
            return {'found': False, 'raw': raw}

        mpesa_code = parts[0]
        try:
            amount = int(parts[4])
        except (ValueError, IndexError):
            amount = 0

        try:
            start_time = int(parts[6]) if len(parts) > 6 else 0
        except (ValueError, IndexError):
            start_time = 0

        return {
            'found': True,
            'mpesa_code': mpesa_code,
            'amount': amount,
            'start_time': start_time,
            'is_unused': start_time == 0,
            'raw': raw,
        }

    except requests.exceptions.RequestException as e:
        logger.error(f'Payment check failed: {e}')
        return {'found': False, 'raw': str(e)}
