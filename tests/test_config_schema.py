"""Tests for Pydantic config schema validation."""
import pytest
from pydantic import ValidationError

from config_schema import CaptureConfig, CaptureDevice, UserConfig


class TestCaptureDevice:
    """Test CaptureDevice schema validation."""

    def test_valid_device_config(self):
        """Valid device config should pass validation."""
        device = CaptureDevice(
            type='RspDuo',
            agcSetPoint=-50,
            gainReduction=40,
            lnaState=4,
            dabNotch=True,
            rfNotch=True,
            bandwidthNumber=0
        )
        assert device.type == 'RspDuo'
        assert device.agcSetPoint == -50
        assert device.gainReduction == 40
        assert device.lnaState == 4

    def test_agc_set_point_must_be_negative_or_zero(self):
        """AGC set point must be <= 0 (dBFS)."""
        # Valid: negative value
        device = CaptureDevice(
            type='RspDuo', agcSetPoint=-60, gainReduction=40,
            lnaState=4, dabNotch=True, rfNotch=True, bandwidthNumber=0
        )
        assert device.agcSetPoint == -60

        # Valid: zero
        device = CaptureDevice(
            type='RspDuo', agcSetPoint=0, gainReduction=40,
            lnaState=4, dabNotch=True, rfNotch=True, bandwidthNumber=0
        )
        assert device.agcSetPoint == 0

        # Invalid: positive value
        with pytest.raises(ValidationError) as exc_info:
            CaptureDevice(
                type='RspDuo', agcSetPoint=10, gainReduction=40,
                lnaState=4, dabNotch=True, rfNotch=True, bandwidthNumber=0
            )
        assert 'less than or equal to 0' in str(exc_info.value)

    def test_gain_reduction_bounds(self):
        """Gain reduction must be 20-59 dB."""
        # Valid: minimum
        device = CaptureDevice(
            type='RspDuo', agcSetPoint=-50, gainReduction=20,
            lnaState=4, dabNotch=True, rfNotch=True, bandwidthNumber=0
        )
        assert device.gainReduction == 20

        # Valid: maximum
        device = CaptureDevice(
            type='RspDuo', agcSetPoint=-50, gainReduction=59,
            lnaState=4, dabNotch=True, rfNotch=True, bandwidthNumber=0
        )
        assert device.gainReduction == 59

        # Invalid: below minimum
        with pytest.raises(ValidationError) as exc_info:
            CaptureDevice(
                type='RspDuo', agcSetPoint=-50, gainReduction=19,
                lnaState=4, dabNotch=True, rfNotch=True, bandwidthNumber=0
            )
        assert 'greater than or equal to 20' in str(exc_info.value)

        # Invalid: above maximum
        with pytest.raises(ValidationError) as exc_info:
            CaptureDevice(
                type='RspDuo', agcSetPoint=-50, gainReduction=60,
                lnaState=4, dabNotch=True, rfNotch=True, bandwidthNumber=0
            )
        assert 'less than or equal to 59' in str(exc_info.value)

    def test_lna_state_bounds(self):
        """LNA state must be 1-9."""
        # Valid: minimum (max gain)
        device = CaptureDevice(
            type='RspDuo', agcSetPoint=-50, gainReduction=40,
            lnaState=1, dabNotch=True, rfNotch=True, bandwidthNumber=0
        )
        assert device.lnaState == 1

        # Valid: maximum (min gain)
        device = CaptureDevice(
            type='RspDuo', agcSetPoint=-50, gainReduction=40,
            lnaState=9, dabNotch=True, rfNotch=True, bandwidthNumber=0
        )
        assert device.lnaState == 9

        # Invalid: below minimum
        with pytest.raises(ValidationError) as exc_info:
            CaptureDevice(
                type='RspDuo', agcSetPoint=-50, gainReduction=40,
                lnaState=0, dabNotch=True, rfNotch=True, bandwidthNumber=0
            )
        assert 'greater than or equal to 1' in str(exc_info.value)

        # Invalid: above maximum
        with pytest.raises(ValidationError) as exc_info:
            CaptureDevice(
                type='RspDuo', agcSetPoint=-50, gainReduction=40,
                lnaState=10, dabNotch=True, rfNotch=True, bandwidthNumber=0
            )
        assert 'less than or equal to 9' in str(exc_info.value)

    def test_boolean_fields(self):
        """Boolean fields should accept True/False."""
        device = CaptureDevice(
            type='RspDuo', agcSetPoint=-50, gainReduction=40,
            lnaState=4, dabNotch=False, rfNotch=False, bandwidthNumber=0
        )
        assert device.dabNotch is False
        assert device.rfNotch is False

    def test_missing_required_field(self):
        """Missing required fields should raise ValidationError."""
        with pytest.raises(ValidationError):
            CaptureDevice(
                type='RspDuo', agcSetPoint=-50,
                # Missing gainReduction, lnaState, etc.
            )


class TestCaptureConfig:
    """Test CaptureConfig schema validation."""

    def test_valid_capture_config(self):
        """Valid capture config with nested device."""
        config = CaptureConfig(
            fs=4000000,
            fc=503000000,
            device={
                'type': 'RspDuo',
                'agcSetPoint': -50,
                'gainReduction': 40,
                'lnaState': 4,
                'dabNotch': True,
                'rfNotch': True,
                'bandwidthNumber': 0
            }
        )
        assert config.fs == 4000000
        assert config.fc == 503000000
        assert config.device.type == 'RspDuo'

    def test_nested_validation_error(self):
        """Invalid nested device config should raise ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            CaptureConfig(
                fs=4000000,
                fc=503000000,
                device={
                    'type': 'RspDuo',
                    'agcSetPoint': -50,
                    'gainReduction': 100,  # Invalid!
                    'lnaState': 4,
                    'dabNotch': True,
                    'rfNotch': True,
                    'bandwidthNumber': 0
                }
            )
        assert 'gainReduction' in str(exc_info.value) or 'less than or equal to 59' in str(exc_info.value)


class TestUserConfig:
    """Test top-level UserConfig schema."""

    def test_optional_capture(self):
        """Capture section is optional."""
        config = UserConfig()
        assert config.capture is None

    def test_with_capture(self):
        """UserConfig with capture section."""
        config = UserConfig(
            capture={
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
        )
        assert config.capture is not None
        assert config.capture.fs == 4000000
