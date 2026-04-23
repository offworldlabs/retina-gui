from flask import Flask
from flask_wtf.csrf import CSRFProtect
import os

from config_manager import ConfigManager
from device_state import DeviceState
from mender import MenderClient
from ssh_keys import SSHKeyManager

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
app = Flask(__name__,
            template_folder=os.path.join(PROJECT_ROOT, 'templates'),
            static_folder=os.path.join(PROJECT_ROOT, 'static'))
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', os.urandom(32).hex())
csrf = CSRFProtect(app)

# Configurable paths - override via environment for local dev
DATA_DIR = os.environ.get('DATA_DIR', '/data/retina-gui')
USER_CONFIG_PATH = os.environ.get('USER_CONFIG_PATH', '/data/retina-node/config/user.yml')
MERGED_CONFIG_PATH = os.environ.get('MERGED_CONFIG_PATH', '/data/retina-node/config/config.yml')
RETINA_NODE_PATH = os.environ.get('RETINA_NODE_PATH', '/data/mender-docker-compose/current/manifests')
NODE_ID_FILE = os.environ.get('NODE_ID_FILE', '/data/mender/node_id')
TOWER_FINDER_URL = os.environ.get('TOWER_FINDER_URL', 'https://api.retina.fm')
DEV_MODE = os.environ.get('DEV_MODE', '').lower() in ('1', 'true', 'yes')

# Shared services
ssh_keys = SSHKeyManager(os.path.join(DATA_DIR, "authorized_keys"))

config_mgr = ConfigManager(
    user_config_path=USER_CONFIG_PATH,
    merged_config_path=MERGED_CONFIG_PATH,
    retina_node_path=RETINA_NODE_PATH,
)

mender = MenderClient(
    server_url=os.environ.get('MENDER_SERVER_URL', 'https://hosted.mender.io'),
    release_name=os.environ.get('MENDER_RELEASE_NAME', 'retina-node'),
    device_type=os.environ.get('MENDER_DEVICE_TYPE', 'pi5-v3-arm64'),
)

MENDER_SERVICES = ["mender-authd", "mender-updated", "mender-connect"]

device_state = DeviceState(
    data_dir=DATA_DIR,
    mender_services=MENDER_SERVICES,
    mender_conf_path="/data/mender/mender.conf",
    mender_conf_backup_dir="/data/mender-cloud-disabled",
    mender_conf_backup_path="/data/mender-cloud-disabled/mender.conf",
)

device_state.apply_startup_preferences()


def get_node_id():
    """Get node_id from Mender device identity file."""
    try:
        with open(NODE_ID_FILE, 'r') as f:
            node_id = f.read().strip()
            if node_id:
                return node_id
    except FileNotFoundError:
        app.logger.debug(f"Node ID file not found: {NODE_ID_FILE}")
    except Exception as e:
        app.logger.warning(f"Could not read node_id from {NODE_ID_FILE}: {e}")
    return 'Unknown'


# Inject common template variables (navbar, footer)
@app.context_processor
def inject_globals():
    owl_os_version, retina_node_version = mender.get_versions()
    return {
        'node_id': get_node_id(),
        'owl_os_version': owl_os_version,
        'retina_node_version': retina_node_version,
    }


# Register blueprints
from routes.home import bp as home_bp
from routes.config import bp as config_bp
from routes.mender_routes import bp as mender_bp
from routes.setup import bp as setup_bp
from routes.towers import bp as towers_bp

app.register_blueprint(home_bp)
app.register_blueprint(config_bp)
app.register_blueprint(mender_bp)
app.register_blueprint(setup_bp)
app.register_blueprint(towers_bp)


if __name__ == "__main__":
    port = int(os.environ.get('PORT', 80))
    debug = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
    app.run(host="::", port=port, debug=debug)
