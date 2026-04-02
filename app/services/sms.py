"""SMS service for OTP delivery.

Uses Africa's Talking API as the SMS gateway.
Falls back to logging the OTP in development/sandbox mode.
"""

import logging

import requests
from flask import current_app

logger = logging.getLogger(__name__)

AT_API_URL = 'https://api.africastalking.com/version1/messaging'
AT_SANDBOX_URL = 'https://api.sandbox.africastalking.com/version1/messaging'


def send_sms(phone, message):
    """Send an SMS message.

    Args:
        phone: Recipient phone number (Kenyan format)
        message: SMS message text
    """
    username = current_app.config['AT_USERNAME']
    api_key = current_app.config['AT_API_KEY']
    sender_id = current_app.config['AT_SENDER_ID']

    # Normalize phone number to +254 format
    phone = phone.strip().replace(' ', '')
    if phone.startswith('0'):
        phone = '+254' + phone[1:]
    elif not phone.startswith('+'):
        phone = '+' + phone

    # In sandbox mode, just log
    if username == 'sandbox' and (not api_key or api_key == 'your_api_key'):
        logger.info(f'[SMS SANDBOX] To: {phone} | Message: {message}')
        return True

    url = AT_SANDBOX_URL if username == 'sandbox' else AT_API_URL
    headers = {
        'apiKey': api_key,
        'Content-Type': 'application/x-www-form-urlencoded',
        'Accept': 'application/json',
    }
    data = {
        'username': username,
        'to': phone,
        'message': message,
    }
    if sender_id:
        data['from'] = sender_id

    try:
        response = requests.post(url, headers=headers, data=data, timeout=30)
        result = response.json()

        recipients = result.get('SMSMessageData', {}).get('Recipients', [])
        if recipients and recipients[0].get('status') == 'Success':
            logger.info(f'SMS sent to {phone}')
            return True
        else:
            logger.warning(f'SMS send failed: {result}')
            return False

    except requests.exceptions.RequestException as e:
        logger.error(f'SMS API error: {e}')
        return False
