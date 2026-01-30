"""Pytest fixtures for retina-gui tests.

Layered Config System:
- config.yml: Merged output (what the GUI reads for display)
- user.yml: User overrides only (what the GUI writes to)

The GUI reads from config.yml to show actual running values,
but only writes changed values to user.yml.
"""
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
def sample_merged_config():
    """Sample merged config.yml - the full config as actually running.

    This represents what config-merger produces: defaults + user overrides merged.
    The GUI reads from this to display values.
    """
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
def sample_user_config():
    """Sample user.yml - only user overrides, not full config.

    This represents what the user has explicitly changed from defaults.
    The GUI writes to this file.
    """
    return {
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
        'tar1090': {
            'adsb_source': '192.168.8.183,30005,beast_in'
        },
        'truth': {
            'adsb': {
                'tar1090': 'sfo1.retnode.com'
            }
        }
    }


@pytest.fixture
def sample_merged_config_no_node_id(sample_merged_config):
    """Merged config without node_id."""
    config = sample_merged_config.copy()
    config['network'] = {}
    return config


@pytest.fixture
def sample_user_config_no_node_id(sample_user_config):
    """User config without node_id."""
    config = sample_user_config.copy()
    if 'network' in config:
        del config['network']
    return config


@pytest.fixture
def config_files(test_config_dir, sample_merged_config, sample_user_config):
    """Create both config.yml and user.yml files for layered config testing.

    Returns tuple of (user_config_path, merged_config_path).
    """
    user_path = os.path.join(test_config_dir, 'user.yml')
    merged_path = os.path.join(test_config_dir, 'config.yml')

    with open(user_path, 'w') as f:
        yaml.dump(sample_user_config, f)
    with open(merged_path, 'w') as f:
        yaml.dump(sample_merged_config, f)

    return user_path, merged_path


@pytest.fixture
def config_files_no_node_id(test_config_dir, sample_merged_config_no_node_id, sample_user_config_no_node_id):
    """Create config files without node_id."""
    user_path = os.path.join(test_config_dir, 'user.yml')
    merged_path = os.path.join(test_config_dir, 'config.yml')

    with open(user_path, 'w') as f:
        yaml.dump(sample_user_config_no_node_id, f)
    with open(merged_path, 'w') as f:
        yaml.dump(sample_merged_config_no_node_id, f)

    return user_path, merged_path


# Legacy fixture for backwards compatibility
@pytest.fixture
def user_config_file(config_files):
    """Legacy fixture - returns just user config path."""
    user_path, _ = config_files
    return user_path


@pytest.fixture
def user_config_file_no_node_id(config_files_no_node_id):
    """Legacy fixture - returns just user config path without node_id."""
    user_path, _ = config_files_no_node_id
    return user_path


@pytest.fixture
def app_client(temp_dir, config_files, test_manifests_dir):
    """Create Flask test client with layered config environment."""
    user_path, merged_path = config_files

    # Set environment variables before importing app
    os.environ['DATA_DIR'] = temp_dir
    os.environ['USER_CONFIG_PATH'] = user_path
    os.environ['MERGED_CONFIG_PATH'] = merged_path
    os.environ['RETINA_NODE_PATH'] = test_manifests_dir

    # Import app after setting env vars (reload to pick up new paths)
    import importlib
    import app as app_module
    importlib.reload(app_module)

    app_module.app.config['TESTING'] = True
    with app_module.app.test_client() as client:
        yield client


@pytest.fixture
def app_client_no_retina(temp_dir, config_files):
    """Create Flask test client without retina-node installed."""
    user_path, merged_path = config_files
    manifests_dir = os.path.join(temp_dir, 'manifests')
    os.makedirs(manifests_dir, exist_ok=True)
    # No docker-compose.yaml = retina-node not installed

    os.environ['DATA_DIR'] = temp_dir
    os.environ['USER_CONFIG_PATH'] = user_path
    os.environ['MERGED_CONFIG_PATH'] = merged_path
    os.environ['RETINA_NODE_PATH'] = manifests_dir

    import importlib
    import app as app_module
    importlib.reload(app_module)

    app_module.app.config['TESTING'] = True
    with app_module.app.test_client() as client:
        yield client


@pytest.fixture
def app_client_no_node_id(temp_dir, config_files_no_node_id, test_manifests_dir):
    """Create Flask test client with config missing node_id."""
    user_path, merged_path = config_files_no_node_id

    os.environ['DATA_DIR'] = temp_dir
    os.environ['USER_CONFIG_PATH'] = user_path
    os.environ['MERGED_CONFIG_PATH'] = merged_path
    os.environ['RETINA_NODE_PATH'] = test_manifests_dir

    import importlib
    import app as app_module
    importlib.reload(app_module)

    app_module.app.config['TESTING'] = True
    with app_module.app.test_client() as client:
        yield client
