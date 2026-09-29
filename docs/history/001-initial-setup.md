# retina-gui Implementation Plan

## Overview

A lightweight web GUI for the Retina radar node, providing:
- Links to services (blah2, tar1090, adsb2dd)
- SSH public key management
- Future: config editing via user.yml

## Tech Stack

| Component | Choice | Rationale |
|-----------|--------|-----------|
| Backend | Flask (Python) | Simple, minimal deps, Python already on owl-os |
| Templating | Jinja2 | Built into Flask |
| CSS | Bootstrap 5 CDN | No build step, responsive |
| JS | Vanilla | Don't need jQuery for simple UI |
| Storage | JSON in `/data/` | Persists across Mender updates |
| Process | systemd | Native, reliable, no Docker |

## Repo Structure

```
retina-gui/
├── .plans/
│   └── IMPLEMENTATION.md      # This file - planning & notes
├── README.md
├── app.py                     # Main Flask application
├── templates/
│   └── index.html             # Main page template
├── static/                    # (optional) custom CSS/JS if needed
└── systemd/
    └── retina-gui.service     # systemd unit file
```

## Implementation Steps

### Phase 1: Create Repo & Basic Flask App

1. ~~Create repo and connect to remote~~ DONE
2. Create basic Flask app with:
   - Single route `/` serving index page
   - Links to services (blah2:8080, tar1090:8078, adsb2dd:49155)
   - Bootstrap 5 via CDN
3. Create systemd service file

### Phase 2: SSH Key Management

1. Add route `POST /ssh-keys` to add a key
2. Store keys directly in `/data/retina-gui/authorized_keys`
3. Configure SSH to read from this location (in owl-os):
   - Add `AuthorizedKeysFile /data/retina-gui/authorized_keys` to sshd_config
   - Baked into image at build time - no boot scripts needed
4. Display current keys on index page
5. Remove baked-in SSH keys from owl-os build (GUI is the only way to add keys)

### Phase 3: owl-os Integration

1. Add `python3-flask` to owl-os base packages
2. Add retina-gui files to owl-os image build:
   - `/opt/retina-gui/` - app files
   - `/etc/systemd/system/retina-gui.service` - service file
3. Create `/data/retina-gui/` directory structure in owl-os bootstrap
4. Configure sshd_config: `AuthorizedKeysFile /data/retina-gui/authorized_keys`
5. Remove baked-in SSH public keys from image build

### Phase 4: Port Changes (retina-node)

1. Change tar1090 from `network_mode: host` (port 80) to bridge with `ports: "8078:80"`
2. Verify adsb2dd still works (it uses user-provided URL, so no code change needed)

## Port Mapping Summary

| Service | Current | After |
|---------|---------|-------|
| retina-gui | N/A | :80 (retina.local) |
| tar1090 | :80 (host) | :8078 (bridge) |
| blah2 | :8080 | :8080 (unchanged) |
| adsb2dd | :49155 | :49155 (unchanged) |

## Persistence Strategy

| Data | Location | Survives Mender Update? |
|------|----------|------------------------|
| SSH keys | `/data/retina-gui/authorized_keys` | Yes |
| GUI config | `/data/retina-gui/config.json` | Yes |

**SSH Key Flow (no symlinks!):**
1. User opens `retina.local` in browser (fresh device, no SSH access yet)
2. Pastes SSH public key into web form
3. Flask writes key to `/data/retina-gui/authorized_keys`
4. SSH reads directly from `/data/` (configured via `AuthorizedKeysFile` in sshd_config)
5. User can now SSH in

## Future Enhancements (v2)

- Config editing UI (reads/writes user.yml, triggers config-merger)
- System status display (Docker container health)
- WiFi configuration
- Node naming/location settings

## Verification

1. Flash updated owl-os image to Pi
2. Access `http://retina.local` - should show retina-gui
3. Add SSH key via web form
4. Verify SSH access works: `ssh pi@retina.local`
5. Click service links - verify all load correctly
6. Reboot Pi - verify SSH key persists

## Files to Modify

### New repo: retina-gui
- `app.py`
- `templates/index.html`
- `systemd/retina-gui.service`
- `README.md`

### owl-os (integration)
- `configuration/base/common.yml` - add python3-flask
- `plugins/playbooks/os_setup/roles/radar_packages/tasks/main.yml` - add retina-gui setup, configure sshd_config
- `ssh_pub_keys/` - remove baked-in keys (GUI is now the only way to add keys)

### retina-node
- `docker-compose.yml` - change tar1090 port from host:80 to bridge:8078
