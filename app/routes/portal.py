from flask import Blueprint, render_template, request, current_app, Response, jsonify

from app.models import db, WiFiPlan, MacSession, normalize_mac

portal_bp = Blueprint('portal', __name__)


@portal_bp.route('/')
def index():
    """Main captive portal landing page.

    The BRAS/NAS redirects here with query params:
        ?mac=AA:BB:CC:DD:EE:FF  - Client MAC address
        ?ip=192.168.x.x         - Client IP
        &venue=Two Rivers Mall   - Venue name
        &location=Nairobi        - Venue location
        &nasid=...               - NAS identifier

    MAC can also come from X-Forwarded-For or custom headers set by nginx/BRAS.
    """
    venue = request.args.get('venue', current_app.config['DEFAULT_VENUE'])
    location = request.args.get('location', current_app.config['DEFAULT_LOCATION'])

    # Extract MAC address from query params or headers
    mac_raw = (request.args.get('mac', '') or
               request.args.get('client_mac', '') or
               request.args.get('calling_station_id', '') or
               request.headers.get('X-Client-MAC', ''))
    mac = normalize_mac(mac_raw) or ''

    # Check if this MAC already has an active session
    mac_session_info = None
    if mac:
        mac_session = MacSession.query.filter_by(mac_address=mac).first()
        if mac_session and mac_session.is_active:
            mac_session_info = {
                'remaining_minutes': mac_session.remaining_seconds // 60,
                'remaining_data_mb': mac_session.remaining_data_bytes // (1024 * 1024) if mac_session.total_data_bytes > 0 else None,
                'speed_down': mac_session.speed_down_kbps // 1024,
                'credits': len(mac_session.credits),
            }

    plans = WiFiPlan.query.filter_by(active=True, is_free=False).order_by(WiFiPlan.price).all()
    free_plan = WiFiPlan.query.filter_by(is_free=True, active=True).first()

    return render_template(
        'portal.html',
        venue=venue,
        location=location,
        plans=plans,
        free_plan=free_plan,
        mac_address=mac,
        mac_session=mac_session_info,
    )


@portal_bp.route('/api/plans')
def get_plans():
    """Return available plans as JSON."""
    plans = WiFiPlan.query.filter_by(active=True, is_free=False).order_by(WiFiPlan.price).all()
    return jsonify([{
        'id': p.id,
        'name': p.name,
        'slug': p.slug,
        'badge': p.badge,
        'badge_class': p.badge_class,
        'price': p.price,
        'price_label': p.price_label,
        'duration_label': p.duration_label,
        'data_mb': p.data_mb,
        'speed_down_kbps': p.speed_down_kbps,
        'description': p.description,
    } for p in plans])


# --- Captive Network Assistant (CNA) detection endpoints ---

def _portal_params():
    """Common params for portal rendering from CNA endpoints."""
    mac_raw = (request.args.get('mac', '') or
               request.headers.get('X-Client-MAC', ''))
    mac = normalize_mac(mac_raw) or ''
    return dict(
        venue=current_app.config['DEFAULT_VENUE'],
        location=current_app.config['DEFAULT_LOCATION'],
        plans=WiFiPlan.query.filter_by(active=True, is_free=False).order_by(WiFiPlan.price).all(),
        free_plan=WiFiPlan.query.filter_by(is_free=True, active=True).first(),
        mac_address=mac,
        mac_session=None,
    )


@portal_bp.route('/hotspot-detect.html')
def apple_cna():
    """Apple iOS/macOS captive portal detection."""
    return render_template('portal.html', **_portal_params())


@portal_bp.route('/generate_204')
def android_cna():
    """Android captive portal detection."""
    mac = normalize_mac(request.args.get('mac', '')) or ''
    return Response(status=302, headers={
        'Location': f'/?mac={mac}' if mac else '/'
    })


@portal_bp.route('/connecttest.txt')
def windows_ncsi():
    """Windows NCSI captive portal detection."""
    mac = normalize_mac(request.args.get('mac', '')) or ''
    return Response(status=302, headers={
        'Location': f'/?mac={mac}' if mac else '/'
    })


@portal_bp.route('/redirect')
def firefox_cna():
    """Firefox captive portal detection."""
    return Response(status=302, headers={'Location': '/'})


@portal_bp.route('/success.txt')
def firefox_success():
    """Firefox captive portal success check."""
    return Response(status=302, headers={'Location': '/'})


@portal_bp.route('/canonical.html')
def chrome_cna():
    """Chrome captive portal detection."""
    return Response(status=302, headers={'Location': '/'})


@portal_bp.route('/health')
def health():
    """Health check endpoint."""
    return jsonify({'status': 'ok', 'service': 'faiba-captive-portal'})
