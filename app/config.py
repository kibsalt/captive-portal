import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    SECRET_KEY = os.getenv('SECRET_KEY', 'change-me-in-production')

    # Database
    _db_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'data')
    os.makedirs(_db_dir, exist_ok=True)
    _db_path = os.path.join(_db_dir, 'captive_portal.db')
    SQLALCHEMY_DATABASE_URI = os.getenv('DATABASE_URL', f'sqlite:///{_db_path}')
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Portal
    PORTAL_HOST = os.getenv('PORTAL_HOST', '192.168.14.4')
    PORTAL_PORT = int(os.getenv('PORTAL_PORT', 8280))
    DEFAULT_VENUE = os.getenv('DEFAULT_VENUE', 'Two Rivers Mall')
    DEFAULT_LOCATION = os.getenv('DEFAULT_LOCATION', 'Limuru Road, Nairobi')

    # Admin
    ADMIN_USERNAME = os.getenv('ADMIN_USERNAME', 'jtlacs')
    ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'bssadmin+ZTE')

    # RADIUS
    RADIUS_SERVER = os.getenv('RADIUS_SERVER', '192.168.14.4')
    RADIUS_COA_PORT = int(os.getenv('RADIUS_COA_PORT', 3799))
    RADIUS_SECRET = os.getenv('RADIUS_SECRET', 'bssadmin+ZTE').encode()
    RADIUS_NAS_ID = os.getenv('RADIUS_NAS_ID', 'faiba-guest-portal')

    # M-Pesa
    MPESA_CONSUMER_KEY = os.getenv('MPESA_CONSUMER_KEY', '')
    MPESA_CONSUMER_SECRET = os.getenv('MPESA_CONSUMER_SECRET', '')
    MPESA_SHORTCODE = os.getenv('MPESA_SHORTCODE', '174379')
    MPESA_PASSKEY = os.getenv('MPESA_PASSKEY', '')
    MPESA_CALLBACK_URL = os.getenv('MPESA_CALLBACK_URL', '')
    MPESA_ENV = os.getenv('MPESA_ENV', 'sandbox')

    # SMS
    AT_USERNAME = os.getenv('AT_USERNAME', 'sandbox')
    AT_API_KEY = os.getenv('AT_API_KEY', '')
    AT_SENDER_ID = os.getenv('AT_SENDER_ID', 'FaibaWiFi')

    # Free tier
    FREE_SESSION_MINUTES = int(os.getenv('FREE_SESSION_MINUTES', 15))
    FREE_SESSION_MB = int(os.getenv('FREE_SESSION_MB', 50))
    FREE_SESSION_SPEED_KBPS = int(os.getenv('FREE_SESSION_SPEED_KBPS', 2048))

    # External MySQL voucher/billing database
    # This is the existing RADIUS billing DB with M-Pesa SHA256-hashed phone records
    EXTERNAL_DB_HOST = os.getenv('EXTERNAL_DB_HOST', '127.0.0.1')
    EXTERNAL_DB_PORT = int(os.getenv('EXTERNAL_DB_PORT', 3306))
    EXTERNAL_DB_USER = os.getenv('EXTERNAL_DB_USER', 'radius')
    EXTERNAL_DB_PASSWORD = os.getenv('EXTERNAL_DB_PASSWORD', '')
    EXTERNAL_DB_NAME = os.getenv('EXTERNAL_DB_NAME', 'radius')

    # Scheduler
    SCHEDULER_API_ENABLED = True
