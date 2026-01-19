"""Tests for form_utils module."""
import pytest

from config_schema import CaptureConfig, CaptureDevice
from form_utils import schema_to_form_fields, get_field_input_type, get_field_constraints


class TestGetFieldInputType:
    """Test HTML input type mapping."""

    def test_bool_to_checkbox(self):
        """Boolean fields should map to checkbox."""
        field_info = CaptureDevice.model_fields['dabNotch']
        assert get_field_input_type(field_info) == 'checkbox'

    def test_int_to_number(self):
        """Integer fields should map to number."""
        field_info = CaptureDevice.model_fields['gainReduction']
        assert get_field_input_type(field_info) == 'number'

    def test_str_to_text(self):
        """String fields should map to text."""
        field_info = CaptureDevice.model_fields['type']
        assert get_field_input_type(field_info) == 'text'


class TestGetFieldConstraints:
    """Test extraction of field constraints."""

    def test_ge_constraint(self):
        """ge constraint should become min."""
        field_info = CaptureDevice.model_fields['gainReduction']
        constraints = get_field_constraints(field_info)
        assert constraints.get('min') == 20

    def test_le_constraint(self):
        """le constraint should become max."""
        field_info = CaptureDevice.model_fields['gainReduction']
        constraints = get_field_constraints(field_info)
        assert constraints.get('max') == 59

    def test_le_only_constraint(self):
        """Field with only le constraint."""
        field_info = CaptureDevice.model_fields['agcSetPoint']
        constraints = get_field_constraints(field_info)
        assert constraints.get('max') == 0
        assert 'min' not in constraints

    def test_no_constraints(self):
        """Field with no constraints."""
        field_info = CaptureDevice.model_fields['bandwidthNumber']
        constraints = get_field_constraints(field_info)
        assert 'min' not in constraints
        assert 'max' not in constraints


class TestSchemaToFormFields:
    """Test schema to form field conversion."""

    def test_basic_field_conversion(self):
        """Test basic field properties are converted."""
        values = {
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
        fields = schema_to_form_fields(CaptureConfig, values)

        # Should have fs, fc, device
        assert len(fields) == 3

        # Check fs field
        fs_field = next(f for f in fields if f['name'] == 'fs')
        assert fs_field['title'] == 'Sample Rate'
        assert fs_field['type'] == 'number'
        assert fs_field['value'] == 4000000
        assert fs_field['description'] == 'Hz'

    def test_nested_group(self):
        """Nested models should become groups."""
        values = {
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
        fields = schema_to_form_fields(CaptureConfig, values)

        device_field = next(f for f in fields if f['name'] == 'device')
        assert device_field['type'] == 'group'
        assert 'fields' in device_field

        # Check nested fields
        nested_fields = device_field['fields']
        type_field = next(f for f in nested_fields if f['name'] == 'type')
        assert type_field['value'] == 'RspDuo'

    def test_checkbox_field(self):
        """Boolean fields should be checkboxes."""
        values = {
            'fs': 4000000,
            'fc': 503000000,
            'device': {
                'type': 'RspDuo',
                'agcSetPoint': -50,
                'gainReduction': 40,
                'lnaState': 4,
                'dabNotch': True,
                'rfNotch': False,
                'bandwidthNumber': 0
            }
        }
        fields = schema_to_form_fields(CaptureConfig, values)
        device_field = next(f for f in fields if f['name'] == 'device')
        nested_fields = device_field['fields']

        dab_field = next(f for f in nested_fields if f['name'] == 'dabNotch')
        assert dab_field['type'] == 'checkbox'
        assert dab_field['value'] is True

        rf_field = next(f for f in nested_fields if f['name'] == 'rfNotch')
        assert rf_field['type'] == 'checkbox'
        assert rf_field['value'] is False

    def test_constraints_included(self):
        """Field constraints should be included."""
        values = {
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
        fields = schema_to_form_fields(CaptureConfig, values)
        device_field = next(f for f in fields if f['name'] == 'device')
        nested_fields = device_field['fields']

        gain_field = next(f for f in nested_fields if f['name'] == 'gainReduction')
        assert gain_field.get('min') == 20
        assert gain_field.get('max') == 59

    def test_values_from_dict_not_defaults(self):
        """Values should come from provided dict, not schema defaults."""
        # Provide different values than any defaults
        values = {
            'fs': 8000000,  # Different from typical default
            'fc': 100000000,
            'device': {
                'type': 'CustomDevice',
                'agcSetPoint': -30,
                'gainReduction': 25,
                'lnaState': 7,
                'dabNotch': False,
                'rfNotch': False,
                'bandwidthNumber': 5
            }
        }
        fields = schema_to_form_fields(CaptureConfig, values)

        fs_field = next(f for f in fields if f['name'] == 'fs')
        assert fs_field['value'] == 8000000

        device_field = next(f for f in fields if f['name'] == 'device')
        nested_fields = device_field['fields']

        type_field = next(f for f in nested_fields if f['name'] == 'type')
        assert type_field['value'] == 'CustomDevice'

    def test_missing_values_return_none(self):
        """Missing values should return None, not defaults."""
        values = {}  # Empty values
        fields = schema_to_form_fields(CaptureConfig, values)

        fs_field = next(f for f in fields if f['name'] == 'fs')
        assert fs_field['value'] is None

    def test_partial_nested_values(self):
        """Partial nested values should work."""
        values = {
            'fs': 4000000,
            'fc': 503000000,
            'device': {
                'type': 'RspDuo',
                # Missing other fields
            }
        }
        fields = schema_to_form_fields(CaptureConfig, values)
        device_field = next(f for f in fields if f['name'] == 'device')
        nested_fields = device_field['fields']

        type_field = next(f for f in nested_fields if f['name'] == 'type')
        assert type_field['value'] == 'RspDuo'

        gain_field = next(f for f in nested_fields if f['name'] == 'gainReduction')
        assert gain_field['value'] is None
