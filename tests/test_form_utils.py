"""Tests for form_utils module.

Tests the form field generation from flat Pydantic schemas.
"""
import pytest

from config_schema import CaptureFormConfig, LocationFormConfig, AdsbTruthConfig
from form_utils import schema_to_form_fields, get_field_input_type, get_field_constraints


class TestGetFieldInputType:
    """Test HTML input type mapping."""

    def test_bool_to_checkbox(self):
        """Boolean fields should map to checkbox."""
        field_info = CaptureFormConfig.model_fields['device_dabNotch']
        assert get_field_input_type(field_info) == 'checkbox'

    def test_int_to_number(self):
        """Integer fields should map to number."""
        field_info = CaptureFormConfig.model_fields['device_gainReduction']
        assert get_field_input_type(field_info) == 'number'

    def test_str_to_text(self):
        """String fields should map to text."""
        field_info = CaptureFormConfig.model_fields['device_type']
        assert get_field_input_type(field_info) == 'text'

    def test_float_to_number(self):
        """Float fields should map to number."""
        field_info = LocationFormConfig.model_fields['rx_latitude']
        assert get_field_input_type(field_info) == 'number'


class TestGetFieldConstraints:
    """Test extraction of field constraints."""

    def test_ge_constraint(self):
        """ge constraint should become min."""
        field_info = CaptureFormConfig.model_fields['device_gainReduction']
        constraints = get_field_constraints(field_info)
        assert constraints.get('min') == 20

    def test_le_constraint(self):
        """le constraint should become max."""
        field_info = CaptureFormConfig.model_fields['device_gainReduction']
        constraints = get_field_constraints(field_info)
        assert constraints.get('max') == 59

    def test_le_only_constraint(self):
        """Field with only le constraint."""
        field_info = CaptureFormConfig.model_fields['device_agcSetPoint']
        constraints = get_field_constraints(field_info)
        assert constraints.get('max') == 0
        assert 'min' not in constraints

    def test_no_constraints(self):
        """Field with no constraints."""
        field_info = CaptureFormConfig.model_fields['device_bandwidthNumber']
        constraints = get_field_constraints(field_info)
        assert 'min' not in constraints
        assert 'max' not in constraints

    def test_gt_constraint(self):
        """gt constraint should become min."""
        field_info = AdsbTruthConfig.model_fields['delay_tolerance']
        constraints = get_field_constraints(field_info)
        assert constraints.get('min') == 0  # gt=0 means > 0

    def test_float_step(self):
        """Float fields should have step='any'."""
        field_info = LocationFormConfig.model_fields['rx_latitude']
        constraints = get_field_constraints(field_info)
        assert constraints.get('step') == 'any'


class TestSchemaToFormFields:
    """Test schema to form field conversion for flat schemas."""

    def test_basic_field_conversion(self):
        """Test basic field properties are converted."""
        values = {
            'fs': 4000000,
            'fc': 503000000,
            'device_type': 'RspDuo',
            'device_agcSetPoint': -50,
            'device_gainReduction': 40,
            'device_lnaState': 4,
            'device_dabNotch': True,
            'device_rfNotch': True,
            'device_bandwidthNumber': 0
        }
        fields = schema_to_form_fields(CaptureFormConfig, values)

        # Should have all flat fields
        assert len(fields) == 9

        # Check fs field
        fs_field = next(f for f in fields if f['name'] == 'fs')
        assert fs_field['title'] == 'Sample Rate'
        assert fs_field['type'] == 'number'
        assert fs_field['value'] == 4000000
        assert fs_field['description'] == 'Hz'

    def test_checkbox_field(self):
        """Boolean fields should be checkboxes."""
        values = {
            'fs': 4000000,
            'fc': 503000000,
            'device_type': 'RspDuo',
            'device_agcSetPoint': -50,
            'device_gainReduction': 40,
            'device_lnaState': 4,
            'device_dabNotch': True,
            'device_rfNotch': False,
            'device_bandwidthNumber': 0
        }
        fields = schema_to_form_fields(CaptureFormConfig, values)

        dab_field = next(f for f in fields if f['name'] == 'device_dabNotch')
        assert dab_field['type'] == 'checkbox'
        assert dab_field['value'] is True

        rf_field = next(f for f in fields if f['name'] == 'device_rfNotch')
        assert rf_field['type'] == 'checkbox'
        assert rf_field['value'] is False

    def test_constraints_included(self):
        """Field constraints should be included."""
        values = {'device_gainReduction': 40}
        fields = schema_to_form_fields(CaptureFormConfig, values)

        gain_field = next(f for f in fields if f['name'] == 'device_gainReduction')
        assert gain_field.get('min') == 20
        assert gain_field.get('max') == 59

    def test_values_from_dict_not_defaults(self):
        """Values should come from provided dict, not schema defaults."""
        values = {
            'fs': 8000000,  # Different from typical default
            'fc': 100000000,
            'device_type': 'CustomDevice',
            'device_agcSetPoint': -30,
            'device_gainReduction': 25,
            'device_lnaState': 7,
            'device_dabNotch': False,
            'device_rfNotch': False,
            'device_bandwidthNumber': 5
        }
        fields = schema_to_form_fields(CaptureFormConfig, values)

        fs_field = next(f for f in fields if f['name'] == 'fs')
        assert fs_field['value'] == 8000000

        type_field = next(f for f in fields if f['name'] == 'device_type')
        assert type_field['value'] == 'CustomDevice'

    def test_missing_values_return_none(self):
        """Missing values should return None."""
        values = {}  # Empty values
        fields = schema_to_form_fields(CaptureFormConfig, values)

        fs_field = next(f for f in fields if f['name'] == 'fs')
        assert fs_field['value'] is None

    def test_partial_values(self):
        """Partial values should work."""
        values = {
            'fs': 4000000,
            # Missing other fields
        }
        fields = schema_to_form_fields(CaptureFormConfig, values)

        fs_field = next(f for f in fields if f['name'] == 'fs')
        assert fs_field['value'] == 4000000

        fc_field = next(f for f in fields if f['name'] == 'fc')
        assert fc_field['value'] is None

    def test_location_form_fields(self):
        """Test location form field conversion."""
        values = {
            'rx_latitude': 37.7644,
            'rx_longitude': -122.3954,
            'rx_altitude': 23,
            'rx_name': '150 Mississippi',
            'tx_latitude': 37.49917,
            'tx_longitude': -121.87222,
            'tx_altitude': 783,
            'tx_name': 'KSCZ-LD'
        }
        fields = schema_to_form_fields(LocationFormConfig, values)

        # Check rx latitude field
        lat_field = next(f for f in fields if f['name'] == 'rx_latitude')
        assert lat_field['type'] == 'number'
        assert lat_field['value'] == 37.7644
        assert lat_field.get('min') == -90
        assert lat_field.get('max') == 90

        # Check rx name field
        name_field = next(f for f in fields if f['name'] == 'rx_name')
        assert name_field['type'] == 'text'
        assert name_field['value'] == '150 Mississippi'

    def test_adsb_truth_form_fields(self):
        """Test ADS-B truth form field conversion."""
        values = {
            'enabled': True,
            'tar1090': 'sfo1.retnode.com',
            'adsb2dd': 'localhost:49155',
            'delay_tolerance': 2.0,
            'doppler_tolerance': 5.0
        }
        fields = schema_to_form_fields(AdsbTruthConfig, values)

        # Check enabled field
        enabled_field = next(f for f in fields if f['name'] == 'enabled')
        assert enabled_field['type'] == 'checkbox'
        assert enabled_field['value'] is True

        # Check delay_tolerance field (float with gt constraint)
        delay_field = next(f for f in fields if f['name'] == 'delay_tolerance')
        assert delay_field['type'] == 'number'
        assert delay_field['value'] == 2.0
        assert delay_field.get('step') == 'any'

    def test_readonly_field(self):
        """Fields with readonly=True should have readonly in output."""
        values = {'fs': 4000000, 'device_type': 'RspDuo'}
        fields = schema_to_form_fields(CaptureFormConfig, values)

        # device_type has readonly=True in schema
        type_field = next(f for f in fields if f['name'] == 'device_type')
        assert type_field.get('readonly') is True

        # fs does not have readonly
        fs_field = next(f for f in fields if f['name'] == 'fs')
        assert fs_field.get('readonly') is False

        # dabNotch does not have readonly
        dab_field = next(f for f in fields if f['name'] == 'device_dabNotch')
        assert dab_field.get('readonly') is False
