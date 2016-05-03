#
# Copyright (c) 2013, 2014, 2016 NORDUnet A/S. All rights reserved.
# Copyright 2012 Roland Hedberg. All rights reserved.
#
# See the file eduid-IdP/LICENSE.txt for license statement.
#
# Author : Fredrik Thulin <fredrik@thulin.net>
#          Roland Hedberg
#

import pprint
from cgi import escape

import eduid_idp
from saml2.request import AuthnRequest
from saml2.sigver import verify_redirect_signature
from saml2.s_utils import UnravelError


class SSOLoginData(object):
    """
    Class to hold data about an ongoing login process - i.e. data relating to a
    particular IdP visitor in the process of logging in, but not yet fully logged in.

    :param key: unique reference for this instance
    :param req_info: pysaml2 AuthnRequest data
    :param data: dict

    :type key: string
    :type req_info: AuthnRequest
    :type data: dict
    :type binding: string
    """
    def __init__(self, key, req_info, data, binding):
        self._key = key
        self._req_info = req_info
        self._SAMLRequest = data['SAMLRequest']
        self._RelayState = data.get('RelayState', '')
        self._FailCount = data.get('FailCount', 0)
        self._binding = binding

    def __str__(self):
        data = self.to_dict()
        if 'SAMLRequest' in data:
            data['SAMLRequest length'] = len(data['SAMLRequest'])
            del data['SAMLRequest']
        return pprint.pformat(data)

    def to_dict(self):
        res = {'key': self._key,
               'req_info': self._req_info,
               'SAMLRequest': self._SAMLRequest,
               'RelayState': self._RelayState,
               'binding': self._binding,
               'FailCount': self._FailCount,
               }
        return res

    @property
    def key(self):
        """
        Unique reference for this instance. Used for storing SSOLoginData instances
        in SSOLoginDataCache.
        :rtype: string
        """
        return escape(self._key, quote=True)

    @property
    def SAMLRequest(self):
        """
        The SAML request in transport encoding (base 64).

        :rtype : string
        """
        return escape(self._SAMLRequest, quote=True)

    @property
    def req_info(self):
        """
        req_info is SAMLRequest, but parsed

        :rtype: AuthnRequest
        """
        return self._req_info

    @property
    def RelayState(self):
        """
        This is an opaque string generated by a SAML SP that must be sent to the
        SP when the authentication is finished and the user redirected to the SP.

        :rtype: string
        """
        return escape(self._RelayState, quote=True)

    @property
    def FailCount(self):
        """
        The number of failed login attempts. Used to show an alert message to the
        user to make them aware of the reason they got back to the IdP login page.

        :rtype: int
        """
        return self._FailCount

    @FailCount.setter
    def FailCount(self, value):
        """
        Set the FailCount.

        :param value: new value
        :type value: int
        """
        assert isinstance(value, int)
        self._FailCount = value

    @property
    def binding(self):
        """
        binding this request was received with

        :rtype: string
        """
        return escape(self._binding, quote=True)


class SSOLoginDataCache(object):
    """
    Login data is state kept between rendering the login screen, to when the user is
    completely logged in and redirected from the IdP to the original resource the
    user is accessing.

    :param idp_app: saml2.server.Server() instance
    :param name: string describing this cache
    :param logger: logging logger
    :param ttl: expire time of data in seconds
    :param config: IdP configuration data
    :param lock: threading.Lock() instance

    :type idp_app: saml2.server.Server
    :type name: string
    :type logger: logging.Logger
    :type ttl: int
    :type config: IdPConfig
    :type lock: threading.Lock
    """

    def __init__(self, idp_app, name, logger, ttl, config, lock = None):
        assert isinstance(config, eduid_idp.config.IdPConfig)
        self.IDP = idp_app
        self.logger = logger
        self.config = config
        if (config.redis_sentinel_hosts or config.redis_host) and config.session_app_key:
            self._cache = eduid_idp.cache.ExpiringCacheCommonSession(name, logger, ttl, config)
        else:
            self._cache = eduid_idp.cache.ExpiringCacheMem(name, logger, ttl, lock)
        logger.debug('Set up IDP ticket cache {!s}'.format(self._cache))

    def store_ticket(self, ticket):
        """
        Add an entry to the IDP.ticket cache.

        :param ticket: SSOLoginData instance
        :returns: True on success
        """
        self.logger.debug('Storing login state (IdP ticket) in {!r}:\n{!s}'.format(self._cache, ticket))
        self._cache.add(ticket.key, ticket)
        return True

    def create_ticket(self, data, binding, key=None):
        """
        Create an SSOLoginData instance from a dict.

        The dict must contain SAMLRequest and is typically

        {'RelayState': '/path',
         'SAMLRequest': 'nVLB...==',
         ...
        }

        :param data: dict containing at least `SAMLRequest'.
        :param binding: SAML2 binding as string (typically a URN)
        :param key: unique key to use. If not specified, one will be computed.
        :returns: SSOLoginData instance

        :type data: dict
        :type binding: string
        :type key: string | None
        :rtype: SSOLoginData
        """
        if not binding:
            raise eduid_idp.error.ServiceError("Can't create IdP ticket with unknown binding", logger = self.logger)
        req_info = self._parse_SAMLRequest(data, binding)
        if not key:
            key = self._cache.key(data["SAMLRequest"])
        ticket = SSOLoginData(key, req_info, data, binding)
        self.logger.debug("Created new login state (IdP ticket) for request {!s}".format(key))
        return ticket

    def get_ticket(self, info, binding=None):
        """
        _info is redirect HTTP query parameters.

        Will include a 'key' parameter, or a 'SAMLRequest'.

        :param info: dict containing `key' or `SAMLRequest'.
        :param binding: SAML2 binding (typically a URN)
        :returns: SSOLoginData instance

        :type info: dict
        :type binding: string
        :rtype: SSOLoginData
        """
        if not info:
            raise eduid_idp.error.BadRequest("Bad request, please re-initiate login", logger = self.logger)

        # Try ticket-cache lookup based on key, or key derived from SAMLRequest
        if "key" in info:
            _key = info["key"]
        elif "SAMLRequest" in info:
            _key = self._cache.key(info["SAMLRequest"])
            self.logger.debug("No 'key' in info, hashed SAMLRequest into key {!s}".format(_key))
        else:
            raise eduid_idp.error.BadRequest("Missing SAMLRequest, please re-initiate login",
                                             logger = self.logger, extra = {'info': info, 'binding': binding})

        # lookup
        self.logger.debug("Lookup SSOLoginData (ticket) using key {!r}".format(_key))
        _ticket = self._cache.get(_key)

        if _ticket is None:
            self.logger.debug("Key {!r} not found in IDP.ticket ({!r})".format(_key, self._cache))
            if "key" in info and "SAMLRequest" not in info:
                # raise eduid_idp.error.LoginTimeout("Missing IdP ticket, please re-initiate login",
                #                                   logger = self.logger, extra = {'info': info, 'binding': binding})
                # This error could perhaps be handled better, but the LoginTimeout error message
                # is not the right one for a number of scenarios where this problem has been
                # observed so we'll go with ServiceError for now and try to diagnose it properly.
                raise eduid_idp.error.ServiceError("Login state not found, please re-initiate login",
                                                   logger = self.logger)
            # cache miss, parse SAMLRequest
            if binding is None and 'binding' in info:
                binding = info['binding']
            _ticket = self.create_ticket(info, binding, key=_key)
            self.store_ticket(_ticket)
        else:
            self.logger.debug("Retreived login state (IdP.ticket) :\n{!s}".format(_ticket))

        if isinstance(_ticket, dict):
            # Ticket was stored in a backend that could not natively store a SSOLoginData instance. Recreate.
            _ticket = self.create_ticket(_ticket, _ticket['binding'], key=_key)
            self.logger.debug('Re-created SSOLoginData from stored ticket state:\n{!s}'.format(_ticket))

        return _ticket

    def _parse_SAMLRequest(self, info, binding):
        """
        Parse a SAMLRequest query parameter (base64 encoded) into an AuthnRequest
        instance.

        If the SAMLRequest is signed, the signature is validated and a BadRequest()
        returned on failure.

        :param info: dict with keys 'SAMLRequest' and possibly 'SigAlg' and 'Signature'
        :param binding: SAML binding
        :returns: pysaml2 AuthnRequest information
        :raise: BadRequest if request signature validation fails

        :type info: dict
        :type binding: string
        :rtype: AuthnRequest
        """
        # self.logger.debug("Parsing SAML request : {!r}".format(info["SAMLRequest"]))
        try:
            _req_info = self.IDP.parse_authn_request(info['SAMLRequest'], binding)
        except UnravelError as exc:
            self.logger.info('Failed parsing SAML request ({!s} bytes)'.format(len(info['SAMLRequest'])))
            self.logger.debug('Failed parsing SAML request:\n{!s}\nException {!s}'.format(info['SAMLRequest'], exc))
            raise eduid_idp.error.BadRequest('No valid SAMLRequest found', logger = self.logger)
        if not _req_info:
            # Either there was no request, or pysaml2 found it to be unacceptable.
            # For example, the IssueInstant might have been out of bounds.
            self.logger.debug("No valid SAMLRequest returned by pysaml2")
            raise eduid_idp.error.BadRequest("No valid SAMLRequest found", logger = self.logger)
        assert isinstance(_req_info, AuthnRequest)

        # Only perform expensive parse/pretty-print if debugging
        if self.config.debug:
            xmlstr = eduid_idp.util.maybe_xml_to_string(_req_info.message)
            self.logger.debug("Decoded SAMLRequest into AuthnRequest {!r} :\n\n{!s}\n\n".format(
                _req_info.message, xmlstr))
        try:
            # XXX Temporary debug logging clause. This whole try/except can be removed in the next release.
            self.logger.debug("Verify request signatures: {!r}".format(self.config.verify_request_signatures))
        except AttributeError:
            self.logger.debug("FAILED logging verify request signatures")

        if "SigAlg" in info and "Signature" in info:  # Signed request
            issuer = _req_info.message.issuer.text
            _certs = self.IDP.metadata.certs(issuer, "any", "signing")
            if self.config.verify_request_signatures:
                verified_ok = False
                for cert in _certs:
                    if verify_redirect_signature(info, cert):
                        verified_ok = True
                        break
                if not verified_ok:
                    _key = self._cache.key(info["SAMLRequest"])
                    self.logger.info("{!s}: SAML request signature verification failure".format(_key))
                    raise eduid_idp.error.BadRequest("SAML request signature verification failure",
                                                     logger = self.logger)
            else:
                self.logger.debug("Ignoring existing request signature, verify_request_signature is False")
        else:
            # XXX check if metadata says request should be signed ???
            # Leif says requests are typically not signed, and that verifying signatures
            # on SAML requests is considered a possible DoS attack vector, so it is typically
            # not done.
            # XXX implement configuration flag to disable signature verification
            self.logger.debug("No signature in SAMLRequest")
        return _req_info
