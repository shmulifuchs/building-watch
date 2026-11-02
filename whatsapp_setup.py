"""Save a Whapi token locally without displaying it or sending messages."""
import argparse
import getpass
import json
import os
from pathlib import Path


def save_token(destination, token):
    token = token.strip()
    if not token or any(c.isspace() for c in token):
        raise ValueError('Enter only the API token copied from your Whapi channel.')
    destination = Path(destination).expanduser().absolute()
    if destination.exists():
        raise ValueError('A connection file already exists. No changes were made.')
    # Existing directories and their ignore rules may belong to the user.
    # Secure the credential itself; do not alter other files or permissions.
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(destination), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump({'provider': 'whapi', 'token': token}, stream)
            stream.write('\n')
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--file', type=Path, required=True,
                        help='Private local credentials file, outside the deliverables folder')
    args = parser.parse_args()
    if args.file.name != 'whapi.json':
        parser.error('The credentials filename must be whapi.json. Paste the token only at the private token prompt.')
    print('Paste your Whapi channel API token below. It will not appear as you type.')
    print('This saves the connection key only; it does not contact Whapi or send a message.')
    while True:
        try:
            token = getpass.getpass('Whapi API token (paste now, THEN press Return): ').strip()
        except (KeyboardInterrupt, EOFError):
            parser.exit(1, '\nSetup cancelled. No new key was saved.\n')
        if not token or any(c.isspace() for c in token):
            print('No valid token received. Try again below; you are still in the setup helper.')
            continue
        break
    try:
        path = save_token(args.file, token)
    except ValueError as error:
        parser.exit(1, str(error) + '\n')
    print('Connection key saved privately to: ' + str(path))
    print('Return to the chat and say: Connected and key saved.')


if __name__ == '__main__':
    main()
