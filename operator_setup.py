"""Create a private operator grant file without exposing its access token."""
import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path
from buildingwatch.config import validate


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True)
    parser.add_argument('--file',required=True,type=Path)
    parser.add_argument('--actor',required=True)
    parser.add_argument('--buildings',required=True,nargs='+')
    args=parser.parse_args()
    buildings=validate(json.loads(Path(args.config).read_text())['buildings'])
    if not args.actor.strip() or any(b not in buildings for b in args.buildings):
        parser.error('Choose an operator name and configured building IDs')
    if args.file.exists():
        parser.error('Grant file already exists; no changes were made')
    token=getpass.getpass('Choose an operator access token (at least 24 characters): ')
    if len(token)<24 or token!=getpass.getpass('Enter the same token again: '):
        parser.error('Tokens must match and contain at least 24 characters')
    grants={args.actor:{'token_sha256':hashlib.sha256(token.encode()).hexdigest(),
                        'buildings':args.buildings}}
    args.file.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd=os.open(str(args.file),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as stream:
        json.dump(grants,stream,indent=2)
    print('Private operator grant created. Keep the chosen token in your password manager.')


if __name__=='__main__':
    main()
