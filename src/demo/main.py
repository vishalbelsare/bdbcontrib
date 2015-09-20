# -*- coding: utf-8 -*-

#   Copyright (c) 2010-2014, MIT Probabilistic Computing Project
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

import base64
import bayeslite
import contextlib
import ed25519
import getopt
import json
import requests
import os
import sys
import zlib
from ..version import __version__

#DEMO_URI = 'http://probcomp.csail.mit.edu/bayesdb/demo/current'
DEMO_URI = 'http://127.0.0.1:12345/'
PUBKEY = '\x93\xca\x8f\xedds\x934B\xf8\xac\xee\x91A\x1d\xa9-\xf5\xfb\xe3\xbf\xe4\xea\xba\nG\xa5>z=\xc4\x8b'

short_options = 'hu:v'
long_options = [
    'help',
    'uri=',
    'version',
]

def main():
    # Parse options.
    try:
        opts, args = getopt.getopt(sys.argv[1:], short_options, long_options)
    except getopt.GetoptError as e:
        sys.stderr.write(str(e))
        usage(sys.stderr)
        sys.exit(2)
    demo_uri = DEMO_URI
    pubkey = PUBKEY
    for o, a in opts:
        if o in ('-v', '--version'):
            version(sys.stdout)
            sys.exit(0)
        elif o in ('-h', '--help'):
            usage(sys.stdout)
            sys.exit(0)
        elif o in ('-u', '--uri'):
            demo_uri = a
        else:
            assert False, 'invalid option %s' % (o,)

    # Select command.
    fetch_p = False
    launch_p = False
    if len(args) == 0:
        fetch_p = True
        launch_p = True
    elif args[0] == 'help':
        usage(sys.stdout)
        sys.exit(0)
    elif args[0] == 'version':
        version(sys.stdout)
        sys.exit(0)
    elif args[0] == 'fetch':
        if 1 < len(args):
            sys.stderr.write('%s: excess arguments\n' % (sys.argv[0],))
        fetch_p = True
    elif args[0] == 'launch':
        if 1 < len(args):
            sys.stderr.write('%s: excess arguments\n' % (sys.argv[0],))
        launch_p = True
    else:
        sys.stderr.write('%s: invalid command: %s\n' % (sys.argv[0], args[0]))
        usage(sys.stderr)
        sys.exit(2)

    # Fetch demo if requested.
    if fetch_p:
        if 0 < len(os.listdir(os.curdir)):
            sys.stderr.write('Please enter an empty directory first!\n')
            sys.exit(2)
        nretry = 3
        last_error = None
        while 0 < nretry:
            try:
                download_demo(demo_uri, pubkey)
            except Exception as e:
                last_error = e
                nretry -= 1
            else:
                break
        if last_error is not None:
            sys.stderr.write(str(last_error))
            sys.exit(1)

    # Launch demo if requested.
    if launch_p:
        try:
            os.execlp('ipython', 'ipython', 'notebook')
        except Exception as e:
            sys.stderr.write(str(e))
            sys.stderr.write('Failed to launch ipython!\n')
            sys.exit(1)

def usage(out):
    out.write('Usage: %s [-hv] [-u <uri>]\n' % (sys.argv[0],))
    out.write('       %s [-hv] [-u <uri>] fetch\n' % (sys.argv[0],))
    out.write('       %s [-hv] [-u <uri>] launch\n' % (sys.argv[0],))

def version(out):
    out.write('bdbcontrib %s\n' % (__version__,))
    out.write('bayeslite %s\n' % (bayeslite.__version__,))

class Fail(Exception):
    def __init__(self, string):
        self._string = string
    def __str__(self):
        return 'Failed to download demo: %s' % (self._string,)

def fail(s):
    raise Fail(s)
def bad(s):
    # XXX Distinguish MITM on network from local failure?
    fail(s)

def selftest():
    payload = 'x\x9c\xab\xae\x05\x00\x01u\x00\xf9'
    try:
        if zlib.decompress(payload) != '{}':
            raise Exception
    except Exception:
        fail('Compression self-test failed!')
    sig = 'R6i&2\x911)\xce9Y\x0b&\xd2\xb0-<\xa5\rw\xc4)\xd6\xd4\x89\x03\x10\x8a;\x1e)\xfe\xb0\x92\xca?\xc3\x17\x0c\xc1\x84\xdd\xe6\xb2\xbfDZ\xe7Z\xd6*y\xe99\x9fk\x1e\xb9\x0f`\x07\xc0\x83\x08'
    try:
        ed25519.checkvalid(sig, payload, PUBKEY)
    except:
        fail('Crypto self-test failed!')

def download_demo(demo_uri, pubkey):
    with note('Requesting') as progress:
        headers = {
            'User-Agent': 'bdbcontrib demo'
        }
        r = requests.get(demo_uri, headers=headers, stream=True)
        try:
            content_length = int(r.headers['content-length'])
        except Exception:
            bad('Bad content-length!')
        if content_length > 64*1024*1024:
            bad('Demo too large!')
        try:
            sig = r.iter_content(chunk_size=64, decode_unicode=False).next()
        except Exception:
            bad('Invalid signature!')
        try:
            chunks = []
            so_far = 64
            for chunk in r.iter_content(chunk_size=1024):
                if content_length - so_far < len(chunk):
                    raise Exception
                so_far += len(chunk)
                progress(so_far, content_length)
                chunks.append(chunk)
            # Doesn't matter if content-length overestimates.
            payload = ''.join(chunks)
        except Exception:
            bad('Invalid payload!')
    with note('Verifying'):
        selftest()
        try:
            ed25519.checkvalid(sig, payload, pubkey)
        except Exception:
            with open('/tmp/riastradh/bad.pack', 'wb') as f:
                f.write(sig)
                f.write(payload)
            bad('Signature verification failed!')
    with note('Decompressing'):
        try:
            demo_json = zlib.decompress(payload)
        except Exception:
            fail('Decompression failed!')
    with note('Parsing'):
        try:
            demo = json.loads(demo_json)
        except Exception:
            fail('Parsing failed!')
    for filename, data_b64 in sorted(demo.iteritems()):
        with note('Decoding %s' % (filename,)):
            try:
                data = base64.b64decode(data_b64)
            except Exception:
                fail('Failed to decode file: %s' % (filename,))
        with note('Extracting %s' % (filename,)):
            try:
                with open(filename, 'wb') as f:
                    f.write(data)
            except Exception:
                fail('Failed to write file: %s' % (filename,))

@contextlib.contextmanager
def note(head):
    start = '%s...' % (head,)
    sys.stdout.write(start)
    sys.stdout.flush()
    def progress(n, d):
        if os.isatty(sys.stdout.fileno()):
            sys.stdout.write('\r%s %d/%d' % (start, n, d))
            sys.stdout.flush()
    yield progress
    sys.stdout.write(' done\n')
    sys.stdout.flush()