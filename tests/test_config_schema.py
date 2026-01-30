"""Tests for Pydantic config schema validation.

Tests the flat form schemas used for validation:
- CaptureFormConfig (flat capture settings)
- LocationFormConfig (flat location settings)
- AdsbTruthConfig (ADS-B truth matching)
- Tar1090Config (tar1090 viewer settings)

Also tests the layered config utility functions.
"""
import os
import tempfile
import pytest
from pydantic import ValidationError

from config_schema import (
    CaptureFormConfig, LocationFormConfig, AdsbTruthConfig, Tar1090Config,
    load_yaml_file, save_yaml_file, values_differ
)


class TestCaptureFormConfig:
    """Test CaptureFormConfig (flat) schema validation."""

    def test_valid_capture_config(self):
        """Valid capture config should pass validation."""
        config = CaptureFormConfig(
            fs=2000000,
            fc=503000000,
            device_type='RspDuo',
            device_agcSetPoint=-50,
            device_gainReduction=40,
            device_lnaState=4,
            device_dabNotch=True,
            device_rfNotch=True,
            device_bandwidthNumber=0
        )
        assert config.fs == 2000000
        assert config.device_type == 'RspDuo'
        assert config.device_gainReduction == 40

    def test_agc_set_point_must_be_negative_or_zero(self):
        """AGC set point must be <= 0 (dBFS)."""
        # Valid: negative value
        config = CaptureFormConfig(
            fs=2000000, fc=503000000, device_type='RspDuo',
            device_agcSetPoint=-60, device_gainReduction=40,
            device_lnaState=4, device_dabNotch=True,
            device_rfNotch=True, device_bandwidthNumber=0
        )
        assert config.device_agcSetPoint == -60

        # Valid: zero
        config = CaptureFormConfig(
            fs=2000000, fc=503000000, device_type='RspDuo',
            device_agcSetPoint=0, device_gainReduction=40,
            device_lnaState=4, device_dabNotch=True,
            device_rfNotch=True, device_bandwidthNumber=0
        )
        assert config.device_agcSetPoint == 0

        # Invalid: positive value
        with pytest.raises(ValidationError) as exc_info:
            CaptureFormConfig(
                fs=2000000, fc=503000000, device_type='RspDuo',
                device_agcSetPoint=10, device_gainReduction=40,
                device_lnaState=4, device_dabNotch=True,
                device_rfNotch=True, device_bandwidthNumber=0
            )
        assert 'less than or equal to 0' in str(exc_info.value)

    def test_gain_reduction_bounds(self):
        """Gain reduction must be 20-59 dB."""
        # Valid: minimum
        config = CaptureFormConfig(
            fs=2000000, fc=503000000, device_type='RspDuo',
            device_agcSetPoint=-50, device_gainReduction=20,
            device_lnaState=4, device_dabNotch=True,
            device_rfNotch=True, device_bandwidthNumber=0
        )
        assert config.device_gainReduction == 20

        # Valid: maximum
        config = CaptureFormConfig(
            fs=2000000, fc=503000000, device_type='RspDuo',
            device_agcSetPoint=-50, device_gainReduction=59,
            device_lnaState=4, device_dabNotch=True,
            device_rfNotch=True, device_bandwidthNumber=0
        )
        assert config.device_gainReduction == 59

        # Invalid: below minimum
        with pytest.raises(ValidationError) as exc_info:
            CaptureFormConfig(
                fs=2000000, fc=503000000, device_type='RspDuo',
                device_agcSetPoint=-50, device_gainReduction=19,
                device_lnaState=4, device_dabNotch=True,
                device_rfNotch=True, device_bandwidthNumber=0
            )
        assert 'greater than or equal to 20' in str(exc_info.value)

        # Invalid: above maximum
        with pytest.raises(ValidationError) as exc_info:
            CaptureFormConfig(
                fs=2000000, fc=503000000, device_type='RspDuo',
                device_agcSetPoint=-50, device_gainReduction=60,
                device_lnaState=4, device_dabNotch=True,
                device_rfNotch=True, device_bandwidthNumber=0
            )
        assert 'less than or equal to 59' in str(exc_info.value)

    def test_lna_state_bounds(self):
        """LNA state must be 1-9."""
        # Valid: minimum (max gain)
        config = CaptureFormConfig(
            fs=2000000, fc=503000000, device_type='RspDuo',
            device_agcSetPoint=-50, device_gainReduction=40,
            device_lnaState=1, device_dabNotch=True,
            device_rfNotch=True, device_bandwidthNumber=0
        )
        assert config.device_lnaState == 1

        # Valid: maximum (min gain)
        config = CaptureFormConfig(
            fs=2000000, fc=503000000, device_type='RspDuo',
            device_agcSetPoint=-50, device_gainReduction=40,
            device_lnaState=9, device_dabNotch=True,
            device_rfNotch=True, device_bandwidthNumber=0
        )
        assert config.device_lnaState == 9

        # Invalid: below minimum
        with pytest.raises(ValidationError) as exc_info:
            CaptureFormConfig(
                fs=2000000, fc=503000000, device_type='RspDuo',
                device_agcSetPoint=-50, device_gainReduction=40,
                device_lnaState=0, device_dabNotch=True,
                device_rfNotch=True, device_bandwidthNumber=0
            )
        assert 'greater than or equal to 1' in str(exc_info.value)

        # Invalid: above maximum
        with pytest.raises(ValidationError) as exc_info:
            CaptureFormConfig(
                fs=2000000, fc=503000000, device_type='RspDuo',
                device_agcSetPoint=-50, device_gainReduction=40,
                device_lnaState=10, device_dabNotch=True,
                device_rfNotch=True, device_bandwidthNumber=0
            )
        assert 'less than or equal to 9' in str(exc_info.value)


class TestLocationFormConfig:
    """Test LocationFormConfig validation."""

    def test_valid_location(self):
        """Valid location should pass validation."""
        config = LocationFormConfig(
            rx_latitude=37.7644, rx_longitude=-122.3954,
            rx_altitude=23, rx_name='150 Mississippi',
            tx_latitude=37.49917, tx_longitude=-121.87222,
            tx_altitude=783, tx_name='KSCZ-LD'
        )
        assert config.rx_latitude == 37.7644
        assert config.tx_name == 'KSCZ-LD'

    def test_latitude_bounds(self):
        """Latitude must be -90 to 90."""
        # Invalid: > 90
        with pytest.raises(ValidationError):
            LocationFormConfig(
                rx_latitude=91, rx_longitude=0,
                rx_altitude=0, rx_name='Test',
                tx_latitude=0, tx_longitude=0,
                tx_altitude=0, tx_name='Test'
            )

        # Invalid: < -90
        with pytest.raises(ValidationError):
            LocationFormConfig(
                rx_latitude=-91, rx_longitude=0,
                rx_altitude=0, rx_name='Test',
                tx_latitude=0, tx_longitude=0,
                tx_altitude=0, tx_name='Test'
            )

    def test_longitude_bounds(self):
        """Longitude must be -180 to 180."""
        # Invalid: > 180
        with pytest.raises(ValidationError):
            LocationFormConfig(
                rx_latitude=0, rx_longitude=181,
                rx_altitude=0, rx_name='Test',
                tx_latitude=0, tx_longitude=0,
                tx_altitude=0, tx_name='Test'
            )

        # Invalid: < -180
        with pytest.raises(ValidationError):
            LocationFormConfig(
                rx_latitude=0, rx_longitude=-181,
                rx_altitude=0, rx_name='Test',
                tx_latitude=0, tx_longitude=0,
                tx_altitude=0, tx_name='Test'
            )


class TestAdsbTruthConfig:
    """Test ADS-B truth config validation."""

    def test_valid_config(self):
        """Valid config should pass."""
        config = AdsbTruthConfig(
            enabled=True, tar1090='server.com',
            adsb2dd='localhost:49155',
            delay_tolerance=2.0, doppler_tolerance=5.0
        )
        assert config.enabled is True
        assert config.delay_tolerance == 2.0

    def test_tolerance_must_be_positive(self):
        """Tolerances must be > 0."""
        with pytest.raises(ValidationError):
            AdsbTruthConfig(
                enabled=True, tar1090='x', adsb2dd='x',
                delay_tolerance=0, doppler_tolerance=5.0
            )
        with pytest.raises(ValidationError):
            AdsbTruthConfig(
                enabled=True, tar1090='x', adsb2dd='x',
                delay_tolerance=-1, doppler_tolerance=5.0
            )


class TestTar1090Config:
    """Test tar1090 config validation."""

    def test_valid_config(self):
        """Valid config should pass."""
        config = Tar1090Config(
            adsb_source_host='192.168.1.1', adsb_source_port=30005,
            adsb_source_protocol='beast_in',
            adsblol_fallback=True, adsblol_radius=40
        )
        assert config.adsb_source_port == 30005

    def test_port_bounds(self):
        """Port must be 1-65535."""
        with pytest.raises(ValidationError):
            Tar1090Config(
                adsb_source_host='x', adsb_source_port=0,
                adsb_source_protocol='x',
                adsblol_fallback=True, adsblol_radius=40
            )
        with pytest.raises(ValidationError):
            Tar1090Config(
                adsb_source_host='x', adsb_source_port=65536,
                adsb_source_protocol='x',
                adsblol_fallback=True, adsblol_radius=40
            )

    def test_radius_bounds(self):
        """Radius must be 1-500."""
        with pytest.raises(ValidationError):
            Tar1090Config(
                adsb_source_host='x', adsb_source_port=30005,
                adsb_source_protocol='x',
                adsblol_fallback=True, adsblol_radius=0
            )
        with pytest.raises(ValidationError):
            Tar1090Config(
                adsb_source_host='x', adsb_source_port=30005,
                adsb_source_protocol='x',
                adsblol_fallback=True, adsblol_radius=501
            )


class TestYamlIO:
    """Test YAML file loading and saving."""

    def test_load_missing_file(self):
        """Loading missing file should return empty dict."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, 'missing.yml')
            result = load_yaml_file(path)
            assert result == {}

    def test_save_and_load_roundtrip(self):
        """Saved config should load back correctly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, 'test.yml')
            data = {
                'capture': {'fs': 2000000},
                'location': {'rx': {'latitude': 37.7644}}
            }
            save_yaml_file(path, data)
            loaded = load_yaml_file(path)
            assert loaded['capture']['fs'] == 2000000
            assert loaded['location']['rx']['latitude'] == 37.7644


class TestValuesDiffer:
    """Test values_differ utility function."""

    def test_same_values(self):
        """Same values should not differ."""
        assert not values_differ(42, 42)
        assert not values_differ('test', 'test')
        assert not values_differ(True, True)

    def test_different_values(self):
        """Different values should differ."""
        assert values_differ(42, 43)
        assert values_differ('test', 'other')
        assert values_differ(True, False)

    def test_none_handling(self):
        """None values should be handled correctly."""
        assert not values_differ(None, None)
        assert values_differ(None, 42)
        assert values_differ(42, None)

    def test_float_comparison(self):
        """Float comparison should handle precision."""
        assert not values_differ(1.0, 1.0)
        assert not values_differ(1.0, 1.0000000001)  # Within tolerance
        assert values_differ(1.0, 2.0)
