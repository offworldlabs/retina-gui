"""Pytest fixtures for retina-gui tests."""
import os
import tempfile
import pytest
import yaml


@pytest.fixture
def temp_dir():
    """Create a temporary directory for test files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def test_config_dir(temp_dir):
    """Create config directory structure for testing."""
    config_dir = os.path.join(temp_dir, 'config')
    os.makedirs(config_dir, exist_ok=True)
    return config_dir


@pytest.fixture
def test_manifests_dir(temp_dir):
    """Create manifests directory with docker-compose.yaml."""
    manifests_dir = os.path.join(temp_dir, 'manifests')
    os.makedirs(manifests_dir, exist_ok=True)
    # Create docker-compose.yaml so retina-node appears "installed"
    with open(os.path.join(manifests_dir, 'docker-compose.yaml'), 'w') as f:
        f.write('# dummy\n')
    return manifests_dir


@pytest.fixture
def sample_user_config():
    """Sample valid user config with all sections."""
    return {
        'capture': {
            'fs': 4000000,
            'fc': 503000000,
            'device': {
                'type': 'RspDuo',
                'agcSetPoint': -50,
                'gainReduction': 40,
                'lnaState': 4,
                'dabNotch': True,
                'rfNotch': True,
                'bandwidthNumber': 0
            }
        },
        'network': {
            'node_id': 'ret7dd2cb0d'
        },
        'location': {
            'rx': {
                'latitude': 37.7644,
                'longitude': -122.3954,
                'altitude': 23,
                'name': '150 Mississippi'
            },
            'tx': {
                'latitude': 37.49917,
                'longitude': -121.87222,
                'altitude': 783,
                'name': 'KSCZ-LD'
            }
        },
        'truth': {
            'adsb': {
                'enabled': True,
                'tar1090': 'sfo1.retnode.com',
                'adsb2dd': 'localhost:49155',
                'delay_tolerance': 2.0,
                'doppler_tolerance': 5.0
            }
        },
        'tar1090': {
            'adsb_source': '192.168.8.183,30005,beast_in',
            'adsblol_fallback': True,
            'adsblol_radius': 40
        }
    }


@pytest.fixture
def sample_user_config_no_node_id():
    """Sample config without node_id."""
    return {
        'capture': {
            'fs': 4000000,
            'fc': 503000000,
            'device': {
                'type': 'RspDuo',
                'agcSetPoint': -50,
                'gainReduction': 40,
                'lnaState': 4,
                'dabNotch': True,
                'rfNotch': True,
                'bandwidthNumber': 0
            }
        }
    }


@pytest.fixture
def user_config_file(test_config_dir, sample_user_config):
    """Create a user.yml file with sample config."""
    config_path = os.path.join(test_config_dir, 'user.yml')
    with open(config_path, 'w') as f:
        yaml.dump(sample_user_config, f)
    return config_path


@pytest.fixture
def user_config_file_no_node_id(test_config_dir, sample_user_config_no_node_id):
    """Create a user.yml file without node_id."""
    config_path = os.path.join(test_config_dir, 'user.yml')
    with open(config_path, 'w') as f:
        yaml.dump(sample_user_config_no_node_id, f)
    return config_path


@pytest.fixture
def app_client(temp_dir, user_config_file, test_manifests_dir):
    """Create Flask test client with test environment."""
    # Set environment variables before importing app
    os.environ['DATA_DIR'] = temp_dir
    os.environ['USER_CONFIG_PATH'] = user_config_file
    os.environ['RETINA_NODE_PATH'] = test_manifests_dir

    # Import app after setting env vars (reload to pick up new paths)
    import importlib
    import app as app_module
    importlib.reload(app_module)

    app_module.app.config['TESTING'] = True
    with app_module.app.test_client() as client:
        yield client


@pytest.fixture
def app_client_no_retina(temp_dir, user_config_file):
    """Create Flask test client without retina-node installed."""
    manifests_dir = os.path.join(temp_dir, 'manifests')
    os.makedirs(manifests_dir, exist_ok=True)
    # No docker-compose.yaml = retina-node not installed

    os.environ['DATA_DIR'] = temp_dir
    os.environ['USER_CONFIG_PATH'] = user_config_file
    os.environ['RETINA_NODE_PATH'] = manifests_dir

    import importlib
    import app as app_module
    importlib.reload(app_module)

    app_module.app.config['TESTING'] = True
    with app_module.app.test_client() as client:
        yield client


@pytest.fixture
def app_client_no_node_id(temp_dir, user_config_file_no_node_id, test_manifests_dir):
    """Create Flask test client with config missing node_id."""
    os.environ['DATA_DIR'] = temp_dir
    os.environ['USER_CONFIG_PATH'] = user_config_file_no_node_id
    os.environ['RETINA_NODE_PATH'] = test_manifests_dir

    import importlib
    import app as app_module
    importlib.reload(app_module)

    app_module.app.config['TESTING'] = True
    with app_module.app.test_client() as client:
        yield client
