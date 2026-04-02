from flask import Blueprint, render_template, request, current_app, Response, jsonify

from app.models import db, WiFiPlan

portal_bp = Blueprint('portal', __name__)


@portal_bp.route('/')
def index():
    """Main captive portal landing page."""
    venue = request.args.get('venue', current_app.config['DEFAULT_VENUE'])
    location = request.args.get('location', current_app.config['DEFAULT_LOCATION'])
    plans = WiFiPlan.query.filter_by(active=True, is_free=False).order_by(WiFiPlan.price).all()
    free_plan = WiFiPlan.query.filter_by(is_free=True, active=True).first()

    return render_template(
        'portal.html',
        venue=venue,
        location=location,
        plans=plans,
        free_plan=free_plan,
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

@portal_bp.route('/hotspot-detect.html')
def apple_cna():
    """Apple iOS/macOS captive portal detection.
    Apple expects '<HTML><BODY>Success</BODY></HTML>' for no captive portal.
    Returning anything else triggers the CNA sign-in sheet.
    """
    return render_template('portal.html',
                           venue=current_app.config['DEFAULT_VENUE'],
                           location=current_app.config['DEFAULT_LOCATION'],
                           plans=WiFiPlan.query.filter_by(active=True, is_free=False).order_by(WiFiPlan.price).all(),
                           free_plan=WiFiPlan.query.filter_by(is_free=True, active=True).first())


@portal_bp.route('/generate_204')
def android_cna():
    """Android captive portal detection.
    Android expects HTTP 204 for no captive portal.
    Returning 302 redirect triggers the sign-in page.
    """
    return Response(status=302, headers={'Location': '/'})


@portal_bp.route('/connecttest.txt')
def windows_ncsi():
    """Windows NCSI captive portal detection.
    Windows expects 'Microsoft Connect Test' for no captive portal.
    Returning redirect triggers the sign-in browser.
    """
    return Response(status=302, headers={'Location': '/'})


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
