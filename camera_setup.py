"""Connect Building Watch to an ANNKE or Reolink camera on this network and save the settings privately.

The password is entered at a hidden prompt and saved only in an owner-readable file
inside the ignored .local/ folder. For a Reolink camera on HTTPS, the certificate fingerprint
is recorded on first connection and checked on every later connection. ANNKE cameras use
digest sign-in, so the password itself does not cross the network.
"""
import argparse
import getpass
import json
import os
import sys
from pathlib import Path

from buildingwatch.annke import AnnkeClient
from buildingwatch.reolink import (CameraError, CameraUnsupported, ReolinkClient,
                                   certificate_fingerprint)

ROOT = Path(__file__).resolve().parent


def save_settings(destination, settings, replace=False):
    destination = Path(destination).expanduser().absolute()
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if destination.exists() and not replace:
        raise ValueError('Camera settings already exist. Use --replace to overwrite them.')
    temporary = destination.with_name(destination.name + '.new')
    fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(settings, stream, indent=2)
            stream.write('\n')
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return destination


def check_camera(settings):
    """Log in, take one snapshot and read person detection. Returns a list of result lines."""
    if settings.get('brand') == 'annke':
        return check_annke(settings)
    fields = {k: v for k, v in settings.items() if k not in ('building', 'brand')}
    client = ReolinkClient(**fields)
    client.login()
    lines = ['Signed in to the camera.']
    lines.append('Live picture: ' + ('usable' if client.usable_frame() else 'NOT usable (check lens, lighting and stream)'))
    try:
        lines.append('Person detection: ' + ('someone in view right now' if client.person_detected()
                                             else 'working, nobody in view'))
    except CameraUnsupported:
        raise CameraError('This camera does not report person detection through its API. '
                          'Turn on smart/person detection in the Reolink app and try again.') from None
    return lines


def check_annke(settings):
    fields = {k: v for k, v in settings.items() if k not in ('building', 'brand', 'retention_hours')}
    client = AnnkeClient(**fields)
    lines = ['Signed in to the camera: ' + client.login()]
    lines.append('Live picture: ' + ('usable' if client.usable_frame() else 'NOT usable (check lens, lighting and stream)'))
    try:
        client.start_events()
        deadline = client.clock() + 5
        while not client.events_connected and not client.events_unsupported and client.clock() < deadline:
            client.closed.wait(0.1)
        if client.events_unsupported:
            raise CameraError('This camera does not offer detection events. Turn on motion detection '
                              'in the Annke Vision app or the camera web page and try again.')
        if not client.events_connected:
            raise CameraError('Could not connect to the camera detection events.')
        lines.append('Detection events: connected. On a C800 these usually mean motion, not a confirmed person.')
    finally:
        client.close()
    return lines


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--brand', choices=['annke', 'reolink'], default='annke')
    parser.add_argument('--host', required=True, help="Camera's IP address on this network, e.g. 192.168.1.64")
    parser.add_argument('--building', default='office-1', help='Building id from buildings.json')
    parser.add_argument('--username', default='admin')
    parser.add_argument('--scheme', choices=['https', 'http'],
                        help='Reolink only: https (default) or http if HTTPS is turned off')
    parser.add_argument('--port', type=int)
    parser.add_argument('--channel', type=int, help='Camera channel; leave out for a single camera')
    parser.add_argument('--file', type=Path, default=ROOT / '.local' / 'camera.json')
    parser.add_argument('--replace', action='store_true', help='Overwrite existing camera settings')
    args = parser.parse_args()

    if args.file.exists() and not args.replace:
        parser.exit(1, 'Camera settings already exist at {}. Use --replace to overwrite them.\n'.format(args.file))
    annke = args.brand == 'annke'
    if annke and args.scheme == 'https':
        parser.exit(1, 'ANNKE setup uses digest sign-in over http; leave out --scheme.\n')
    scheme = 'http' if annke else args.scheme or 'https'
    channel = args.channel if args.channel is not None else 1 if annke else 0
    settings = dict(brand=args.brand, building=args.building, host=args.host, scheme=scheme,
                    username=args.username, channel=channel)
    if args.port:
        settings['port'] = args.port
    try:
        if annke:
            print('If this camera is new, first set its password on its web page or in SADP/Annke Vision.')
        elif scheme == 'https':
            fingerprint = certificate_fingerprint(args.host, args.port or 443)
            print('Camera certificate fingerprint (SHA-256):')
            print('  ' + ':'.join(fingerprint[i:i + 2] for i in range(0, len(fingerprint), 2)))
            print('This will be trusted from now on; a different certificate will be refused.')
            settings['fingerprint_sha256'] = fingerprint
        else:
            print('Warning: http sends the camera password unencrypted on your local network.')
        try:
            settings['password'] = getpass.getpass('Camera password (hidden, then press Return): ')
        except (KeyboardInterrupt, EOFError):
            parser.exit(1, '\nSetup cancelled. Nothing was saved.\n')
        if not settings['password']:
            parser.exit(1, 'No password entered. Nothing was saved.\n')
        for line in check_camera(settings):
            print(line)
        path = save_settings(args.file, settings, args.replace)
    except (CameraError, OSError, ValueError) as error:
        print('Camera setup did not finish: ' + str(error), file=sys.stderr)
        print('Nothing was saved.', file=sys.stderr)
        return 1
    print('Camera settings saved privately to: ' + str(path))
    print('Start the connector with: ./watch camera')
    if annke:
        print('Start recording and clip creation with: ./watch record')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
