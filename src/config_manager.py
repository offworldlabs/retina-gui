"""Configuration management for retina-gui.

Handles the layered config system:
  default.yml -> user.yml -> forced.yml -> config.yml (merged)

This class:
  - READS from config.yml (merged) to show actual running values
  - WRITES to user.yml (only values that differ from merged config)
"""

import os

from config_schema import (
    load_yaml_file, save_yaml_file, values_differ
)


class ConfigManager:
    """Manages layered configuration: reading merged config, writing user overrides."""

    def __init__(self, user_config_path, merged_config_path, retina_node_path):
        self.user_config_path = user_config_path
        self.merged_config_path = merged_config_path
        self.retina_node_path = retina_node_path

    def is_retina_node_installed(self):
        """Check if retina-node stack is deployed."""
        return os.path.exists(os.path.join(self.retina_node_path, 'docker-compose.yaml'))

    def load_merged_config(self):
        """Load merged config.yml (what's actually running)."""
        return load_yaml_file(self.merged_config_path)

    def load_user_config(self):
        """Load user.yml (user overrides only)."""
        return load_yaml_file(self.user_config_path)

    def save_user_config(self, config):
        """Save user overrides to user.yml."""
        save_yaml_file(self.user_config_path, config)

    @staticmethod
    def flatten_capture_for_form(nested):
        """Convert nested capture config to flat form values."""
        if not nested:
            return {}
        device = nested.get('device', {}) or {}
        return {
            'fs': nested.get('fs'),
            'fc': nested.get('fc'),
            'device_type': device.get('type'),
            'device_agcSetPoint': device.get('agcSetPoint'),
            'device_gainReduction': device.get('gainReduction'),
            'device_lnaState': device.get('lnaState'),
            'device_dabNotch': device.get('dabNotch'),
            'device_rfNotch': device.get('rfNotch'),
            'device_bandwidthNumber': device.get('bandwidthNumber'),
        }

    @staticmethod
    def unflatten_capture_from_form(flat):
        """Convert flat form values to nested capture config."""
        return {
            'fs': flat.get('fs'),
            'fc': flat.get('fc'),
            'device': {
                'type': flat.get('device_type'),
                'agcSetPoint': flat.get('device_agcSetPoint'),
                'gainReduction': flat.get('device_gainReduction'),
                'lnaState': flat.get('device_lnaState'),
                'dabNotch': flat.get('device_dabNotch', False),
                'rfNotch': flat.get('device_rfNotch', False),
                'bandwidthNumber': flat.get('device_bandwidthNumber'),
            }
        }

    @staticmethod
    def flatten_location_for_form(nested):
        """Convert nested location config to flat form values."""
        if not nested:
            return {}
        rx = nested.get('rx', {}) or {}
        tx = nested.get('tx', {}) or {}
        return {
            'rx_latitude': rx.get('latitude'),
            'rx_longitude': rx.get('longitude'),
            'rx_altitude': rx.get('altitude'),
            'rx_name': rx.get('name'),
            'tx_latitude': tx.get('latitude'),
            'tx_longitude': tx.get('longitude'),
            'tx_altitude': tx.get('altitude'),
            'tx_name': tx.get('name'),
        }

    @staticmethod
    def unflatten_location_from_form(flat):
        """Convert flat form values to nested location config."""
        return {
            'rx': {
                'latitude': flat.get('rx_latitude'),
                'longitude': flat.get('rx_longitude'),
                'altitude': flat.get('rx_altitude'),
                'name': flat.get('rx_name'),
            },
            'tx': {
                'latitude': flat.get('tx_latitude'),
                'longitude': flat.get('tx_longitude'),
                'altitude': flat.get('tx_altitude'),
                'name': flat.get('tx_name'),
            }
        }

    @staticmethod
    def parse_tar1090_adsb_source(config):
        """Split adsb_source string into separate fields for the form."""
        tar1090 = config.get('tar1090', {}) or {}
        adsb_source = tar1090.get('adsb_source', '')

        if adsb_source and ',' in adsb_source:
            parts = adsb_source.split(',', 2)
            return {
                'adsb_source_host': parts[0] if len(parts) > 0 else '',
                'adsb_source_port': int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None,
                'adsb_source_protocol': parts[2] if len(parts) > 2 else '',
                'adsblol_fallback': tar1090.get('adsblol_fallback'),
                'adsblol_radius': tar1090.get('adsblol_radius'),
            }
        return {
            'adsb_source_host': None,
            'adsb_source_port': None,
            'adsb_source_protocol': None,
            'adsblol_fallback': tar1090.get('adsblol_fallback'),
            'adsblol_radius': tar1090.get('adsblol_radius'),
        }

    @staticmethod
    def format_validation_errors(validation_error, section_prefix):
        """Convert Pydantic ValidationError to dict of field -> error message."""
        errors = {}
        for error in validation_error.errors():
            field_path = section_prefix + '.' + '.'.join(str(loc) for loc in error['loc'])
            errors[field_path] = error['msg']
        return errors

    @staticmethod
    def parse_flat_form_data(form_data):
        """Parse flat form data (capture.field_name, location.field_name) into section dicts."""
        capture = {}
        location = {}
        truth = {}
        tar1090 = {}

        for key, value in form_data.items():
            if value == '':
                continue
            # Parse value
            if value.lower() in ('true', 'false', 'on'):
                parsed = value.lower() in ('true', 'on')
            else:
                try:
                    if '.' in value:
                        parsed = float(value)
                    else:
                        parsed = int(value)
                except ValueError:
                    parsed = value

            # Route to correct section
            if key.startswith('capture.'):
                capture[key[8:]] = parsed  # Remove 'capture.' prefix
            elif key.startswith('location.'):
                location[key[9:]] = parsed  # Remove 'location.' prefix
            elif key.startswith('truth.'):
                truth[key[6:]] = parsed  # Remove 'truth.' prefix
            elif key.startswith('tar1090.'):
                tar1090[key[8:]] = parsed  # Remove 'tar1090.' prefix

        # Handle unchecked checkboxes (they don't get submitted)
        # Only add checkbox defaults if there's other capture data (not just checkboxes)
        capture_has_data = any(k not in ('device_dabNotch', 'device_rfNotch') for k in capture)
        if capture_has_data:
            if 'device_dabNotch' not in capture:
                capture['device_dabNotch'] = False
            if 'device_rfNotch' not in capture:
                capture['device_rfNotch'] = False

        if truth and 'enabled' not in truth:
            truth['enabled'] = False
        if tar1090 and 'adsblol_fallback' not in tar1090:
            tar1090['adsblol_fallback'] = False

        return capture, location, truth, tar1090

    def compute_user_overrides(self, submitted_nested, merged_config, existing_user_config, section_key):
        """Compare submitted values against merged config, return only values that differ.

        Only writes to user.yml values that the user explicitly changed from
        the defaults/merged config.
        """
        merged_section = merged_config.get(section_key, {}) or {}
        existing_section = existing_user_config.get(section_key, {}) or {}

        def find_changes(submitted, merged, existing):
            """Recursively find changed values."""
            changes = {}
            for key, submitted_val in submitted.items():
                merged_val = merged.get(key) if merged else None
                existing_val = existing.get(key) if existing else None

                if isinstance(submitted_val, dict):
                    nested_changes = find_changes(
                        submitted_val,
                        merged_val if isinstance(merged_val, dict) else {},
                        existing_val if isinstance(existing_val, dict) else {}
                    )
                    if nested_changes:
                        changes[key] = nested_changes
                else:
                    if values_differ(submitted_val, merged_val):
                        changes[key] = submitted_val
                    elif existing_val is not None and not values_differ(existing_val, submitted_val):
                        changes[key] = submitted_val

            return changes

        changes = find_changes(submitted_nested, merged_section, existing_section)
        return changes if changes else None
