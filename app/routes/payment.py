import logging
from datetime import datetime

from flask import Blueprint, request, jsonify, current_app

from app.models import db, WiFiPlan, GuestSession, Payment
from app.services.session_manager import activate_session

logger = logging.getLogger(__name__)

payment_bp = Blueprint('payment', __name__)


@payment_bp.route('/initiate', methods=['POST'])
def initiate_payment():
    """Initiate a payment for a WiFi plan.

    Request JSON:
        plan_id: int - WiFi plan ID
        method: str - Payment method (mpesa, airtel, tkash, card, etc.)
        phone: str - Phone number or account number
        mac_address: str (optional) - Device MAC address
        ip_address: str (optional) - Device IP address
    """
    data = request.get_json()
    if not data:
        return jsonify({'error': 'Request body required'}), 400

    plan_id = data.get('plan_id')
    method = data.get('method')
    phone = data.get('phone', '').strip()

    if not plan_id or not method:
        return jsonify({'error': 'plan_id and method are required'}), 400

    plan = WiFiPlan.query.get(plan_id)
    if not plan or not plan.active or plan.is_free:
        return jsonify({'error': 'Invalid plan'}), 400

    if method in ('mpesa', 'airtel', 'tkash', 'sms', 'pesalink') and not phone:
        return jsonify({'error': 'Phone number required for this payment method'}), 400

    # Create guest session
    session = GuestSession(
        phone=phone,
        plan_id=plan.id,
        venue=current_app.config['DEFAULT_VENUE'],
        mac_address=data.get('mac_address', ''),
        ip_address=data.get('ip_address', request.remote_addr),
        status='pending',
    )
    db.session.add(session)
    db.session.flush()

    # Create payment record
    payment = Payment(
        session_id=session.id,
        method=method,
        amount=plan.price,
        phone=phone,
        account_ref=f'FAIBA-{session.id[:8].upper()}',
        status='pending',
    )
    db.session.add(payment)
    db.session.commit()

    # Dispatch to payment provider
    if method == 'mpesa':
        result = _initiate_mpesa(payment, phone, plan)
    elif method in ('airtel', 'tkash'):
        result = _initiate_mobile_money(payment, method, phone, plan)
    elif method == 'card':
        result = _initiate_card(payment, plan)
    elif method == 'voucher':
        return jsonify({'error': 'Use /api/auth/voucher for voucher payments'}), 400
    else:
        # For bank/PesaLink/SMS - simulate pending
        result = {
            'status': 'pending',
            'message': f'Payment initiated via {method}. Please confirm on your device.',
            'session_id': session.id,
            'payment_id': payment.id,
        }

    return jsonify(result)


def _initiate_mpesa(payment, phone, plan):
    """Initiate M-Pesa STK push via Lexabensa gateway."""
    try:
        from app.services.mpesa import stk_push
        result = stk_push(phone=phone, amount=plan.price)

        if result.get('success'):
            payment.status = 'pending'
            payment.status_message = 'STK push sent via Lexabensa'
            db.session.commit()

            return {
                'status': 'pending',
                'message': result['message'],
                'session_id': payment.session_id,
                'payment_id': payment.id,
                'phone': phone,
            }
        else:
            payment.status = 'failed'
            payment.status_message = result.get('message', 'STK push failed')
            db.session.commit()
            return {'status': 'failed', 'message': result.get('message', 'STK push failed')}

    except Exception as e:
        logger.error(f'M-Pesa STK push error: {e}')
        payment.status = 'failed'
        payment.status_message = str(e)
        db.session.commit()
        return {'status': 'failed', 'message': f'Payment error: {e}'}


def _initiate_mobile_money(payment, method, phone, plan):
    """Stub for Airtel Money / T-Kash."""
    payment.status = 'pending'
    payment.status_message = f'{method} payment prompt sent'
    db.session.commit()
    return {
        'status': 'pending',
        'message': f'Payment prompt sent to {phone}. Confirm on your device.',
        'session_id': payment.session_id,
        'payment_id': payment.id,
    }


def _initiate_card(payment, plan):
    """Stub for card payment - would redirect to payment gateway."""
    payment.status = 'pending'
    payment.status_message = 'Awaiting card payment'
    db.session.commit()
    return {
        'status': 'pending',
        'message': 'Card payment gateway initiated.',
        'session_id': payment.session_id,
        'payment_id': payment.id,
    }


@payment_bp.route('/mpesa/callback', methods=['POST'])
def mpesa_callback():
    """M-Pesa Daraja STK push callback (webhook)."""
    data = request.get_json()
    logger.info(f'M-Pesa callback received: {data}')

    if not data:
        return jsonify({'ResultCode': 1, 'ResultDesc': 'No data'}), 400

    body = data.get('Body', {}).get('stkCallback', {})
    checkout_request_id = body.get('CheckoutRequestID')
    result_code = body.get('ResultCode')

    if not checkout_request_id:
        return jsonify({'ResultCode': 1, 'ResultDesc': 'Missing CheckoutRequestID'}), 400

    payment = Payment.query.filter_by(checkout_request_id=checkout_request_id).first()
    if not payment:
        logger.warning(f'Payment not found for CheckoutRequestID: {checkout_request_id}')
        return jsonify({'ResultCode': 0, 'ResultDesc': 'Accepted'})

    if result_code == 0:
        # Payment successful
        metadata = body.get('CallbackMetadata', {}).get('Item', [])
        mpesa_receipt = next((item['Value'] for item in metadata if item['Name'] == 'MpesaReceiptNumber'), None)

        payment.status = 'completed'
        payment.transaction_id = mpesa_receipt
        payment.completed_at = datetime.utcnow()
        payment.status_message = 'Payment confirmed'
        db.session.commit()

        # Activate the WiFi session
        activate_session(payment.session_id)

        logger.info(f'M-Pesa payment completed: {mpesa_receipt} for session {payment.session_id}')
    else:
        payment.status = 'failed'
        payment.status_message = body.get('ResultDesc', 'Payment failed')
        db.session.commit()
        logger.info(f'M-Pesa payment failed: {body.get("ResultDesc")}')

    return jsonify({'ResultCode': 0, 'ResultDesc': 'Accepted'})


@payment_bp.route('/mpesa/check/<int:payment_id>')
def mpesa_check(payment_id):
    """Check M-Pesa payment status via Lexabensa verification API.

    Called by frontend polling after STK push is sent.
    If payment is confirmed and amount matches, auto-activates the WiFi session.
    """
    payment = Payment.query.get(payment_id)
    if not payment:
        return jsonify({'error': 'Payment not found'}), 404

    if payment.status == 'completed':
        return jsonify({
            'status': 'completed',
            'mpesa_code': payment.transaction_id,
            'session_id': payment.session_id,
        })

    if payment.status == 'failed':
        return jsonify({'status': 'failed', 'message': payment.status_message})

    phone = payment.phone
    if not phone:
        return jsonify({'status': 'pending', 'message': 'Waiting for payment...'})

    from app.services.mpesa import check_payment
    result = check_payment(phone)

    if not result.get('found'):
        return jsonify({'status': 'pending', 'message': 'Waiting for M-Pesa confirmation...'})

    # Payment found — verify amount matches
    paid_amount = result.get('amount', 0)
    if paid_amount < payment.amount:
        return jsonify({
            'status': 'pending',
            'message': f'Payment of KES {paid_amount} found but KES {payment.amount} required.',
        })

    # Payment confirmed — activate session
    payment.status = 'completed'
    payment.transaction_id = result.get('mpesa_code', '')
    payment.completed_at = datetime.utcnow()
    payment.status_message = f'M-Pesa confirmed: {result.get("mpesa_code")} (KES {paid_amount})'
    db.session.commit()

    activate_session(payment.session_id)

    logger.info(f'M-Pesa payment confirmed: {result.get("mpesa_code")} KES {paid_amount} for {phone}')

    return jsonify({
        'status': 'completed',
        'mpesa_code': result.get('mpesa_code'),
        'amount': paid_amount,
        'session_id': payment.session_id,
        'message': f'Payment confirmed! Receipt: {result.get("mpesa_code")}',
    })


@payment_bp.route('/confirm', methods=['POST'])
def confirm_payment():
    """Manual payment confirmation (for testing or bank confirmations).

    Request JSON:
        payment_id: int
        transaction_id: str (optional)
    """
    data = request.get_json()
    if not data or not data.get('payment_id'):
        return jsonify({'error': 'payment_id required'}), 400

    payment = Payment.query.get(data['payment_id'])
    if not payment:
        return jsonify({'error': 'Payment not found'}), 404

    if payment.status == 'completed':
        return jsonify({'status': 'already_completed', 'session_id': payment.session_id})

    payment.status = 'completed'
    payment.transaction_id = data.get('transaction_id', f'MANUAL-{payment.id}')
    payment.completed_at = datetime.utcnow()
    payment.status_message = 'Manually confirmed'
    db.session.commit()

    activate_session(payment.session_id)

    return jsonify({
        'status': 'success',
        'message': 'Payment confirmed and session activated',
        'session_id': payment.session_id,
    })


@payment_bp.route('/status/<int:payment_id>')
def payment_status(payment_id):
    """Check payment status."""
    payment = Payment.query.get(payment_id)
    if not payment:
        return jsonify({'error': 'Payment not found'}), 404

    return jsonify({
        'payment_id': payment.id,
        'status': payment.status,
        'message': payment.status_message,
        'session_id': payment.session_id,
        'session_status': payment.session.status if payment.session else None,
    })
