import sys

__author__ = 'rohe0002'

import M2Crypto
import json
import logging
import os
import urlparse

from binascii import b2a_hex
from M2Crypto.__m2crypto import hex_to_bn, bn_to_mpi
from M2Crypto.util import no_passphrase_callback

KEYLOADERR = "Failed to load %s key from '%s' (%s)"
logger = logging.getLogger(__name__)

# ========== base64 encoding/decoding large numbers ====

import string
ALPHABET = string.ascii_uppercase + string.ascii_lowercase +\
           string.digits + '-_'
ALPHABET_REVERSE = dict((c, i) for (i, c) in enumerate(ALPHABET))
BASE = len(ALPHABET)
TB = 2**24
foo = '0000 0001 0000 0000 0000 0001'
foo_b64 = "QAB="

import base64
import struct

def bytes( long_int ):
    bytes = []
    while long_int:
        long_int, r = divmod(long_int, 256)
        bytes.insert(0, r)
    return bytes

def long_to_base64(n):
    bys = bytes(n)
    data = struct.pack('%sB' % len(bys), *bys)
    #xdata = struct.pack('<%sB' % len(bys), *bys)
    if not len(data):
        data = '\x00'
    s = base64.urlsafe_b64encode(data).rstrip('=')
    return s

def b64_set_to_long(s):
    data = base64.urlsafe_b64decode(s + '==')
    n = struct.unpack('>Q', '\x00'* (8-len(data)) + data )
    return n[0]

def base64_to_long(data):
    #if len(data) % 4: # not a multiple of 4
    #    data += '=' * (4 - (len(data) % 4))

    ld = len(data)
    data = str(data)

    lshift = 8 * (3-(ld % 4))

    res = b64_set_to_long(data[0:4])

    if ld > 4:
        if lshift == 24:
            for i in range(4, ld, 4):
                res = (res << 24) + b64_set_to_long(data[i:i+4])
        else:
            for i in range(4, ld-4, 4):
                res = (res << 24) + b64_set_to_long(data[i:i+4])
            i += 4
            res = (res << lshift) + b64_set_to_long(data[i:i+4])

    return res

def long_to_mpi(num):
    """Converts a python integer or long to OpenSSL MPInt used by M2Crypto.
    Borrowed from Snowball.Shared.Crypto"""
    h = hex(num)[2:] # strip leading 0x in string
    if len(h) % 2 == 1:
        h = '0' + h # add leading 0 to get even number of hexdigits
    return bn_to_mpi(hex_to_bn(h)) # convert using OpenSSL BinNum

def mpi_to_long(mpi):
    """Converts an OpenSSL MPint used by M2Crypto to a python integer/long.
    Borrowed from Snowball.Shared.Crypto"""
    return eval("0x%s" % b2a_hex(mpi[4:]))

# ======================================================================

def rsa_eq(key1, key2):
    # Check if two RSA keys are in fact the same
    if key1.n == key2.n and key1.e == key2.e:
        return True
    else:
        return False

def key_eq(key1, key2):
    if type(key1) == type(key2):
        if isinstance(key1, basestring):
            return key1 == key2
        elif isinstance(key1, M2Crypto.RSA.RSA):
            return rsa_eq(key1, key2)

    return False

def rsa_load(filename):
    """Read a PEM-encoded RSA key pair from a file."""
    return M2Crypto.RSA.load_key(filename, M2Crypto.util.no_passphrase_callback)

def rsa_loads(key):
    """Read a PEM-encoded RSA key pair from a string."""
    return M2Crypto.RSA.load_key_string(key,
                                        M2Crypto.util.no_passphrase_callback)

def ec_load(filename):
    return M2Crypto.EC.load_key(filename, M2Crypto.util.no_passphrase_callback)

def x509_rsa_loads(string):
    cert = M2Crypto.X509.load_cert_string(string)
    return cert.get_pubkey().get_rsa()

class RedirectStdStreams(object):
    def __init__(self, stdout=None, stderr=None):
        self._stdout = stdout or sys.stdout
        self._stderr = stderr or sys.stderr

    def __enter__(self):
        self.old_stdout, self.old_stderr = sys.stdout, sys.stderr
        self.old_stdout.flush(); self.old_stderr.flush()
        sys.stdout, sys.stderr = self._stdout, self._stderr

    #noinspection PyUnusedLocal
    def __exit__(self, exc_type, exc_value, traceback):
        self._stdout.flush(); self._stderr.flush()
        sys.stdout = self.old_stdout
        sys.stderr = self.old_stderr


class KeyStore(object):
    use = ["sig", "ver", "enc", "dec"]
    url_types = ["x509_url", "x509_encryption_url", "jwk_url",
                 "jwk_encryption_url"]

    def __init__(self, http_request, keyspecs=None):
        self._store = {}
        self.http_request = http_request

        if keyspecs:
            for keyspec in keyspecs:
                self.add_key(*keyspec)

    def add_key(self, key, type, usage, owner="."):
        """
        :param key: The key
        :param type: Type of key (rsa, ec, hmac, .. )
        :param usage: What to use the key for (signing, verifying, encrypting,
            decrypting
        """

        if owner not in self._store:
            self._store[owner] = {"sig": {}, "ver": {}, "enc": {},
                                  "dec": {}}
            self._store[owner][usage][type] = [key]
        else:
            _keys = self._store[owner][usage]
            try:
                for _key in _keys[type]:
                    if key_eq(_key, key):
                        return

                _keys[type].append(key)
            except KeyError:
                _keys[type] = [key]

    def get_keys(self, usage, type=None, owner="."):
        if not owner:
            res = {}
            for owner, _spec in self._store.items():
                res[owner] = _spec[usage]
            return res
        else:
            try:
                if type:
                    return self._store[owner][usage][type]
                else:
                    return self._store[owner][usage]
            except KeyError:
                return {}

    def pairkeys(self, part):
        """
        Keys for me and someone else.

        :param part: The other part
        :return: dictionary of keys
        """
        _coll = self.keys_by_owner(part)
        if part != ".":
            for usage, spec in self.keys_by_owner(".").items():
                for typ, keys in spec.items():
                    try:
                        _coll[usage][typ].extend(keys)
                    except KeyError:
                        _coll[usage][typ] = keys

        return _coll

    def keys_by_owner(self, owner):
        """
        Get all the keys that belongs to an owner

        :param owner: The name/URL of the owner
        """
        try:
            return self._store[owner]
        except KeyError:
            return {}

    def remove_key_collection(self, owner):
        """
        Remove all keys that belongs to an owner

        :param owner: The name/URL of the owner
        """
        try:
            del self._store[owner]
        except Exception:
            pass

    def remove_key(self, key, owner=".", type=None, usage=None):
        try:
            _keys = self._store[owner]
        except KeyError:
            return

        rem = []
        if usage:
            if type:
                _keys[usage][type].remove(key)
            else:
                for _typ, vals in self._store[owner][usage].items():
                    try:
                        vals.remove(key)
                        if not vals:
                            rem.append((usage, _typ))
                    except Exception:
                        pass
        else:
            for _usage, item in _keys.items():
                if type:
                    _keys[_usage][type].remove(key)
                else:
                    for _typ, vals in _keys[_usage].items():
                        try:
                            vals.remove(key)
                            if not vals:
                                rem.append((_usage, _typ))
                        except Exception:
                            pass

        for _use, _typ in rem:
            del self._store[owner][_use][_typ]
            if not self._store[owner][_use]:
                del self._store[owner][_use]

    def remove_key_type(self, type, owner="."):
        try:
            _keys = self._store[owner]
        except KeyError:
            return

        for _usage in _keys.keys():
            try:
                del self._store[owner][_usage][type]
                if not self._store[owner][_usage]:
                    del self._store[owner][_usage]
            except KeyError:
                pass

    def get_verify_key(self, type="", owner="."):
        return self.get_keys("ver", type, owner)

    def get_sign_key(self, type="", owner="."):
        return self.get_keys("sig", type, owner)

    def get_encrypt_key(self, type="", owner="."):
        return self.get_keys("enc", type, owner)

    def get_decrypt_key(self, type="", owner="."):
        return self.get_keys("dec", type, owner)

    def set_verify_key(self, val, type="hmac", owner="."):
        self.add_key(val, type, "ver", owner)

    def set_sign_key(self, val, type="hmac", owner="."):
        self.add_key(val, type, "sig", owner)

    def set_encrypt_key(self, val, type="hmac", owner="."):
        self.add_key(val, type, "enc", owner)

    def set_decrypt_key(self, val, type="hmac", owner="."):
        self.add_key(val, type, "dec", owner)

    def match_owner(self, url):
        for owner in self._store.keys():
            if url.startswith(owner):
                return owner

        raise Exception("No keys for '%s'" % url)

    def collect_keys(self, url, usage="ver"):
        try:
            owner = self.match_owner(url)
            keys = self.get_keys(usage, owner=owner)
        except Exception:
            keys = None

        try:
            own_keys = self.get_keys(usage)
            if keys:
                for type, key in own_keys.items():
                    keys[type].extend(key)
            else:
                keys = own_keys
        except KeyError:
            pass

        return keys

    def __contains__(self, owner):
        if owner in self._store:
            return True
        else:
            return False

    def has_key_of_type(self, owner, usage, type):
        try:
            _ = self._store[owner][usage][type]
            return True
        except KeyError:
            return False

    def load_x509_cert(self, url, usage, owner):
        """
        Get and transform a X509 cert into a key

        :param url: Where the X509 cert can be found
        :param usage: Assumed usage of the key
        :param owner: The URL of the server
        """
        try:
            r = self.http_request(url, allow_redirects=True)
            if r.status_code == 200:
                cert = str(r.text)
                _key = x509_rsa_loads(cert)
                self.add_key(_key, "rsa", usage, owner)
                return _key
            else:
                raise Exception("HTTP Get error: %s" % r.status_code)
        except Exception, err: # not a RSA key
            logger.warning("Can't load key: %s" % err)
            return None

    def load_jwk(self, url, usage, owner):
        """
        Get and transform a JWK into keys

        :param url: Where the JWK can be found
        :param usage: Assumed usage of the key
        :param owner: The URL of the server
        """
        r = self.http_request(url, allow_redirects=True)
        if r.status_code != 200:
            raise Exception("HTTP Get error: %s" % r.status_code)

        self.loads(r.text, usage, owner)

    def load_keys(self, inst, issuer, replace=False):
        """
        Fetch keys from another server

        :param inst: The provider information
        :param issuer: The provider URL
        :param replace: If all previously gathered keys from this provider
            should be replace.
        """
        for attr in self.url_types:
            if attr in inst:
                if replace:
                    self.remove_key_collection(issuer)
                break

        if "x509_url" in inst and inst["x509_url"]:
            try:
                _verkey = self.load_x509_cert(inst["x509_url"], "ver",
                                              issuer)
            except Exception:
                raise Exception(KEYLOADERR % ('x509', inst["x509_url"]))
        else:
            _verkey = None

        if "x509_encryption_url" in inst and inst["x509_encryption_url"]:
            try:
                self.load_x509_cert(inst["x509_encryption_url"], "enc",
                                    issuer)
            except Exception:
                raise Exception(KEYLOADERR % ('x509_encryption',
                                              inst["x509_encryption_url"]))
        elif _verkey:
            self.set_decrypt_key(_verkey, "rsa", issuer)

        if "jwk_url" in inst and inst["jwk_url"]:
            try:
                _verkeys = self.load_jwk(inst["jwk_url"], "ver", issuer)
            except Exception, err:
                raise Exception(KEYLOADERR % ('jwk', inst["jwk_url"], err))
        else:
            _verkeys = []

        if "jwk_encryption_url" in inst and inst["jwk_encryption_url"]:
            try:
                self.load_jwk(inst["jwk_encryption_url"], "enc", issuer)
            except Exception:
                raise Exception(KEYLOADERR % ('jwk',
                                              inst["jwk_encryption_url"]))
        elif _verkeys:
            for key in _verkeys:
                self.set_decrypt_key(key, "rsa", issuer)

    def update(self, keystore):
        """
        Add keys from another keystore to this keystore

        :param keystore:
        """

        for owner, spec in keystore._store.items():
            if owner == ".":
                continue
            self._store[owner] = spec

    def loads(self, txt, usage, owner):
        """
        Load and create keys from a JWK representation

        Expects something on this form
        {"keys":
            [
                {"alg":"EC",
                 "crv":"P-256",
                 "x":"MKBCTNIcKUSDii11ySs3526iDZ8AiTo7Tu6KPAqv7D4",
                "y":"4Etl6SRW2YiLUrN5vfvVHuhp7x8PxltmWWlbbM4IFyM",
                "use":"enc",
                "kid":"1"},

                {"alg":"RSA",
                "mod": "0vx7agoebGcQSuuPiLJXZptN9nndrQmbXEps2aiAFb....."
                "exp":"AQAB",
                "kid":"2011-04-29"}
            ]
        }

        :param txt: The JWK string representation
        :param usage: Usage if not specified in the JWK
        :param owner: The URL of the server from which the keys where received
        """
        spec = json.loads(txt)
        for kspec in spec["keys"]:
            if kspec["alg"] == "RSA":
                e = base64_to_long(kspec["exp"])
                n = base64_to_long(kspec["mod"])

                k = M2Crypto.RSA.new_pub_key((long_to_mpi(e), long_to_mpi(n)))

#                if "kid" in kspec:
#                    tag = "%s:%s" % ("rsa", kspec["kid"])
#                else:
#                    tag = "rsa"

                self.add_key(k, "rsa", usage, owner)
            elif kspec["alg"] == "HMAC":
                self.add_key(kspec["modulus"], "hmac", usage, owner)

    def dumps(self, usage, type="rsa"):
        """
        Dump to JWK string representation

        :param usage: What the key are expected to be use for
        :param type: The type of key
        :return: The JWK string representation or None
        """
        kspecs = []
        for key in self.get_keys(usage, type):
            if isinstance(key, M2Crypto.RSA.RSA):
                kspecs.append({
                    "alg": "RSA",
                    "mod": long_to_base64(mpi_to_long(key.n)),
                    "exp": long_to_base64(mpi_to_long(key.e)),
                    "use": usage
                })

        if kspecs:
            return json.dumps({"keys": kspecs})
        else:
            return None

    def key_export(self, baseurl, local_path, vault, **kwargs):
        """
        :param baseurl: The base URL to which the key file names are added
        :param local_path: Where on the machine the export files are kept
        :param vault: Where the keys are kept
        :return:
        """
        part = urlparse.urlsplit(baseurl)

        # deal with the export directory
        if part.path.endswith("/"):
            _path = part.path[:-1]
        else:
            _path = part.path[:]

        local_path = proper_path("%s/%s" % (_path,local_path))
        vault_path = proper_path(vault)

        if not os.path.exists(vault_path):
            os.makedirs(vault_path)

        if not os.path.exists(local_path):
            os.makedirs(local_path)

        res = {}
        # For each usage type
        # type, usage, format (rsa, sign, jwt)

        for usage in ["sig", "enc"]:
            if usage in kwargs:
                if kwargs[usage] is None:
                    continue

                _args = kwargs[usage]
                if _args["alg"] == "rsa":
                    try:
                        _key = rsa_load('%s%s' % (vault_path, "pyoidc"))
                    except Exception:
                        devnull = open(os.devnull, 'w')
                        with RedirectStdStreams(stdout=devnull, stderr=devnull):
                            _key = create_and_store_rsa_key_pair(
                                                                path=vault_path)

                    self.add_key(_key, "rsa", usage)
                    if usage == "sig":
                        self.add_key(_key, "rsa", "ver")
                    elif usage == "enc":
                        self.add_key(_key, "rsa", "dec")

                    if "jwk" in _args["format"]:
                            if usage == "sig":
                                _name = ("jwk.json", "jwk_url")
                            else:
                                _name = ("jwk_enc.json", "jwk_encryption_url")

                            # the local filename
                            _export_filename = "%s%s" % (local_path, _name[0])

                            f = open(_export_filename, "w")
                            f.write(self.dumps(usage))
                            f.close()

                            _url = "%s://%s%s" % (part.scheme, part.netloc,
                                                  _export_filename[1:])

                            res[_name[1]] = _url
                    if "x509" in _args["format"]:
                        if usage == "sig":
                            _name = "x509_url"
                        else:
                            _name = "x509_encryption_url"

                        cert, _key = make_cert(2045, "lingon.ladok.umu.se",
                                               _key)
                        # the local filename
                        _export_filename = "%s%s" % (local_path, "cert.pem")
                        cert.save(_export_filename)
                        _url = "%s://%s%s" % (part.scheme, part.netloc,
                                              _export_filename[1:])

                        res[_name] = _url

        return part, res

# ================= create RSA key ======================

def create_and_store_rsa_key_pair(name="pyoidc", path=".", size=1024):
    #Seed the random number generator with 1024 random bytes (8192 bits)
    M2Crypto.Rand.rand_seed(os.urandom(size))

    key = M2Crypto.RSA.gen_key(size, 65537, lambda : None)

    if not path.endswith("/"):
        path += "/"

    key.save_key('%s%s' % (path, name), None, callback=no_passphrase_callback)
    key.save_pub_key('%s%s.pub' % (path, name))

    return key

def proper_path(path):
    """
    Clean up the path specification so it looks like something I could use.
    "./" <path> "/"
    """
    if path.startswith("./"):
        pass
    elif path.startswith("/"):
        path = ".%s" % path
    elif path.startswith("."):
        while path.startswith("."):
            path = path[1:]
        if path.startswith("/"):
            path = ".%s" % path
    else:
        path = "./%s" % path

    if not path.endswith("/"):
        path += "/"

    return path

# ================= create certificate ======================
# heavily influenced by
# http://svn.osafoundation.org/m2crypto/trunk/tests/test_x509.py

import time
from M2Crypto import EVP
from M2Crypto import X509
from M2Crypto import RSA
from M2Crypto import ASN1

def make_req(bits, fqdn="example.com", rsa=None):
    pk = EVP.PKey()
    x = X509.Request()
    if not rsa:
        rsa = RSA.gen_key(bits, 65537, lambda : None)
    pk.assign_rsa(rsa)
    # Because rsa is messed with
    rsa = pk.get_rsa()
    x.set_pubkey(pk)
    name = x.get_subject()
    name.C = "SE"
    name.CN = "OpenID Connect Test Server"
    ext1 = X509.new_extension('subjectAltName', fqdn)
    extstack = X509.X509_Extension_Stack()
    extstack.push(ext1)
    x.add_extensions(extstack)
    x.sign(pk,'sha1')
    return x, pk, rsa

def make_cert(bits, fqdn="example.com", rsa=None):
    req, pk, rsa = make_req(bits, fqdn=fqdn, rsa=rsa)
    pkey = req.get_pubkey()
    sub = req.get_subject()
    cert = X509.X509()
    cert.set_serial_number(1)
    cert.set_version(2)
    cert.set_subject(sub)
    t = long(time.time()) + time.timezone
    now = ASN1.ASN1_UTCTIME()
    now.set_time(t)
    nowPlusYear = ASN1.ASN1_UTCTIME()
    nowPlusYear.set_time(t + 60 * 60 * 24 * 365)
    cert.set_not_before(now)
    cert.set_not_after(nowPlusYear)
    issuer = X509.X509_Name()
    issuer.CN = 'The code tester'
    issuer.O = 'Umea University'
    cert.set_issuer(issuer)
    cert.set_pubkey(pkey)
    cert.sign(pk, 'sha1')
    return cert, rsa
