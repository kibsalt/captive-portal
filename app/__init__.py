import os

from flask import Flask
from flask_apscheduler import APScheduler

from app.config import Config
from app.models import db


scheduler = APScheduler()


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    # Ensure data directory exists
    os.makedirs(os.path.join(app.root_path, '..', 'data'), exist_ok=True)

    # Init extensions
    db.init_app(app)
    scheduler.init_app(app)

    # Register blueprints
    from app.routes.portal import portal_bp
    from app.routes.payment import payment_bp
    from app.routes.auth import auth_bp
    from app.routes.session import session_bp
    from app.routes.radius import radius_bp

    app.register_blueprint(portal_bp)
    app.register_blueprint(payment_bp, url_prefix='/api/payment')
    app.register_blueprint(auth_bp, url_prefix='/api/auth')
    app.register_blueprint(session_bp, url_prefix='/api/session')
    app.register_blueprint(radius_bp, url_prefix='/api/radius')

    with app.app_context():
        db.create_all()
        seed_plans()

    # Start session expiry checker
    from app.services.session_manager import check_expired_sessions
    scheduler.add_job(
        id='check_expired_sessions',
        func=check_expired_sessions,
        trigger='interval',
        seconds=30,
        args=[app]
    )
    scheduler.start()

    return app


def seed_plans():
    from app.models import WiFiPlan
    if WiFiPlan.query.count() == 0:
        plans = [
            WiFiPlan(
                name='Quick Browse', slug='hourly', badge='Hourly', badge_class='',
                price=30, price_label='KES 30/hr',
                duration_seconds=3600, duration_label='1 hour',
                data_mb=500, speed_down_kbps=10240, speed_up_kbps=5120,
                description='500 MB \u00b7 1 hour\n10 Mbps speed'
            ),
            WiFiPlan(
                name='3-Hour Pass', slug='3hour', badge='Value', badge_class='value',
                price=50, price_label='KES 50/3hr',
                duration_seconds=10800, duration_label='3 hours',
                data_mb=1024, speed_down_kbps=15360, speed_up_kbps=7680,
                description='1 GB \u00b7 3 hours\n15 Mbps speed'
            ),
            WiFiPlan(
                name='Day Pass', slug='daily', badge='Popular', badge_class='popular',
                price=100, price_label='KES 100/day',
                duration_seconds=86400, duration_label='24 hours',
                data_mb=2048, speed_down_kbps=20480, speed_up_kbps=10240,
                description='2 GB \u00b7 24 hours\n20 Mbps \u00b7 All sites'
            ),
            WiFiPlan(
                name='Weekend Pass', slug='weekend', badge='Weekend', badge_class='',
                price=150, price_label='KES 150/wknd',
                duration_seconds=172800, duration_label='2 days',
                data_mb=4096, speed_down_kbps=20480, speed_up_kbps=10240,
                description='4 GB \u00b7 2 days\n20 Mbps \u00b7 All sites'
            ),
            WiFiPlan(
                name='Week Pass', slug='weekly', badge='Weekly', badge_class='',
                price=250, price_label='KES 250/wk',
                duration_seconds=604800, duration_label='7 days',
                data_mb=8192, speed_down_kbps=20480, speed_up_kbps=10240,
                description='8 GB \u00b7 7 days\n20 Mbps \u00b7 All sites'
            ),
            WiFiPlan(
                name='Student Plan', slug='student', badge='Student', badge_class='student',
                price=350, price_label='KES 350/mo',
                duration_seconds=2592000, duration_label='30 days',
                data_mb=10240, speed_down_kbps=15360, speed_up_kbps=7680,
                description='10 GB \u00b7 30 days\n15 Mbps \u00b7 All sites'
            ),
            WiFiPlan(
                name='Month Pass', slug='monthly', badge='Monthly', badge_class='',
                price=500, price_label='KES 500/mo',
                duration_seconds=2592000, duration_label='30 days',
                data_mb=20480, speed_down_kbps=20480, speed_up_kbps=10240,
                description='20 GB \u00b7 30 days\n20 Mbps \u00b7 All sites'
            ),
            WiFiPlan(
                name='Unlimited', slug='unlimited', badge='Premium', badge_class='premium',
                price=1000, price_label='KES 1000/mo',
                duration_seconds=2592000, duration_label='30 days',
                data_mb=51200, speed_down_kbps=51200, speed_up_kbps=25600,
                description='50 GB \u00b7 30 days\n50 Mbps \u00b7 Priority'
            ),
            WiFiPlan(
                name='Free Access', slug='free', badge='Free', badge_class='free',
                price=0, price_label='Free',
                duration_seconds=900, duration_label='15 minutes',
                data_mb=50, speed_down_kbps=2048, speed_up_kbps=1024,
                description='50 MB \u00b7 15 minutes\n2 Mbps speed',
                is_free=True
            ),
        ]
        db.session.add_all(plans)
        db.session.commit()
