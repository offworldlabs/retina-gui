# Design history

These are the design plans written before each feature was built, kept as a record of the reasoning at the time. They were not updated as the code changed, so they may not match current behaviour: file names, routes, steps and decisions in them can be out of date or were later reversed.

For how retina-gui works now, read [docs/architecture.md](../architecture.md) and the feature docs in [docs/features/](../features/).

| File | What it planned |
| --- | --- |
| [001-initial-setup.md](001-initial-setup.md) | The first retina-gui: tech stack, service links and SSH public key management. |
| [002-config-gui.md](002-config-gui.md) | The config editor, with forms generated from Pydantic models over `user.yml` (capture settings first). |
| [003_new_config_options.md](003_new_config_options.md) | Adding node ID display, RX/TX location and ADS-B (truth and tar1090) settings to the config page. |
| [005_cloud-services-toggle.md](005_cloud-services-toggle.md) | A Cloud Services toggle that stops the Mender services and backs up their credentials offline. |
| [007-device-state-manager.md](007-device-state-manager.md) | The `DeviceState` class that replaced scattered lock and flag files, fed by Mender state scripts. |
| [007-test-state-scripts.md](007-test-state-scripts.md) | A manual test checklist for the Mender state scripts across each update type. |
| [009-set-up-wizard.md](009-set-up-wizard.md) | The first-boot setup wizard at `/set-up`: agreements, OS update, app install. |
| [010-managed-ota-os-updates.md](010-managed-ota-os-updates.md) | Moving OS updates from standalone `mender-update install` to server-managed deployments. |
| [011-tower-finder-integration.md](011-tower-finder-integration.md) | The wizard's Location step: RX location, tower search via retina-server, TX selection. |
| [013-auto-calibrate-design.md](013-auto-calibrate-design.md) | The Auto-Calibrate design summary across blah2-arm and retina-gui, with its verification tiers. |
| [mender-install-button.md](mender-install-button.md) | An Install Software button that pulls the retina-node artifact from Mender using the device's own auth. |
| [radar-spectrum-mode-toggle.md](radar-spectrum-mode-toggle.md) | The Radar / Spectrum mode switch on the Config page and the bundled spectrum compose file. |
