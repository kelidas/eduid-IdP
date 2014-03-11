#
# Copyright (c) 2013, 2014 NORDUnet A/S. All rights reserved.
# Copyright 2012 Roland Hedberg. All rights reserved.
#
# See the file eduid-IdP/LICENSE.txt for license statement.
#
# Author : Fredrik Thulin <fredrik@thulin.net>
#          Roland Hedberg
#

"""
Code handling Single Sign On logins.
"""

import pprint

from eduid_idp.service import Service
from eduid_idp.sso_session import SSOSession
import eduid_idp.util
import eduid_idp.mischttp

from saml2.request import AuthnRequest

from saml2.s_utils import UnknownPrincipal
from saml2.s_utils import UnsupportedBinding
from saml2.sigver import verify_redirect_signature
from saml2.authn_context import requested_authn_context

from saml2 import BINDING_HTTP_REDIRECT
from saml2 import BINDING_HTTP_POST


class SSOLoginData(object):
    """
    Class to hold data about an ongoing login process - i.e. data relating to a
    particular IdP visitor in the process of logging in, but not yet fully logged in.

    :param key: unique reference for this instance
    :param req_info: pysaml2 AuthnRequest data
    :param data: dict

    :type key: string
    :type req_info: AuthnRequest
    """
    def __init__(self, key, req_info, data):
        self._key = key
        self._req_info = req_info
        self._data = data
        self._FailCount = 0

    def __str__(self):
        return pprint.pformat({'key': self._key,
                               'req_info': self._req_info,
                               'data[:48]': str(self._data)[:48],
                               'FailCount': self._FailCount,
                               })

    @property
    def key(self):
        """
        Unique reference for this instance. Used for storing SSOLoginData instances
        in SSOLoginDataCache.
        :rtype: string
        """
        return self._key

    @property
    def SAMLRequest(self):
        """
        The SAML request in transport encoding (base 64).

        :rtype : string
        """
        return self._data['SAMLRequest']

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
        return self._data.get('RelayState', '')

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


class SSOLoginDataCache(eduid_idp.cache.ExpiringCache):
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
        self.config = config
        eduid_idp.cache.ExpiringCache.__init__(self, name, logger, ttl, lock)

    def store_ticket(self, ticket):
        """
        Add an entry to the IDP.ticket cache.

        :param ticket: SSOLoginData instance
        :returns: True on success
        """
        self.logger.debug("Storing login state (IdP ticket) :\n{!s}".format(ticket))
        self.add(ticket.key, ticket)
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
            key = self.key(data["SAMLRequest"])
        ticket = SSOLoginData(key, req_info, data)
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
            _key = self.key(info["SAMLRequest"])
        else:
            raise eduid_idp.error.BadRequest("Missing SAMLRequest, please re-initiate login",
                                             logger = self.logger, extra = {'info': info, 'binding': binding})

        # lookup
        self.logger.debug("Lookup SSOLoginData (ticket) using key {!r}".format(_key))
        _ticket = self.get(_key)

        if _ticket is None:
            self.logger.debug("Key {!r} not found in IDP.ticket".format(_key))
            if "key" in info:
                raise eduid_idp.error.LoginTimeout("Missing IdP ticket, please re-initiate login",
                                                   logger = self.logger, extra = {'info': info, 'binding': binding})

            # cache miss, parse SAMLRequest
            _ticket = self.create_ticket(info, binding, key=_key)
            self.store_ticket(_ticket)
        else:
            self.logger.debug("Retreived login state (IdP.ticket) :\n{!s}".format(_ticket))

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
        #self.logger.debug("Parsing SAML request : {!r}".format(info["SAMLRequest"]))
        _req_info = self.IDP.parse_authn_request(info["SAMLRequest"], binding)
        assert isinstance(_req_info, AuthnRequest)

        # Only perform expensive parse/pretty-print if debugging
        if self.config.debug:
            xmlstr = eduid_idp.util.maybe_xml_to_string(_req_info.message)
            self.logger.debug("Decoded SAMLRequest into AuthnRequest {!r} :\n\n{!s}\n\n".format(
                _req_info.message, xmlstr))

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
                    _key = self.key(_req_info["SAMLRequest"])
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


# -----------------------------------------------------------------------------
# === Single log in ====
# -----------------------------------------------------------------------------


class SSO(Service):
    """
    Single Sign On service.

    :param session: SSO session
    :param start_response: WSGI-like start_response function pointer
    :param idp_app: IdPApplication instance

    :type session: SSOSession | None
    :type start_response: function
    :type idp_app: idp.IdPApplication
    """

    def __init__(self, session, start_response, idp_app):
        Service.__init__(self, session, start_response, idp_app)
        self.binding = ""
        self.binding_out = None
        self.destination = None

    def perform_login(self, ticket):
        """
        Validate request, and then proceed with creating an AuthnResponse and
        invoking the 'outgoing' SAML2 binding.

        :param ticket: SSOLoginData instance
        :return: Response

        :type ticket: SSOLoginData
        :rtype: string
        """
        assert isinstance(ticket, SSOLoginData)
        assert isinstance(self.sso_session, eduid_idp.sso_session.SSOSession)

        self.logger.debug("\n\n---\n\n")
        self.logger.debug("--- In SSO.perform_login() ---")

        resp_args = self._validate_login_request(ticket)

        user = self.sso_session.idp_user

        response_authn = self._get_login_response_authn(ticket, user)

        attributes = self._make_scoped_eppn(user.identity)

        # Only perform expensive parse/pretty-print if debugging
        if self.config.debug:
            self.logger.debug("Creating an AuthnResponse: user {!r}\n\nAttributes:\n{!s},\n\n"
                              "Response args:\n{!s},\n\nAuthn:\n{!s}\n".format(
                              user,
                              pprint.pformat(attributes),
                              pprint.pformat(resp_args),
                              pprint.pformat(response_authn)))

        saml_response = self.IDP.create_authn_response(attributes, userid = user.username,
                                                       authn = response_authn, sign_assertion = True,
                                                       **resp_args)

        # Only perform expensive parse/pretty-print if debugging
        if self.config.debug:
            # saml_response is a compact XML document as string. For debugging, it is very
            # useful to get that pretty-printed in the logfile directly, so parse the XML
            # string to an etree again and then pretty-print format the etree into a new string.
            xmlstr = eduid_idp.util.maybe_xml_to_string(saml_response)
            self.logger.debug("Created AuthNResponse :\n\n{!s}\n\n".format(xmlstr))

        # Create the Javascript self-posting form that will take the user back to the SP
        # with a SAMLResponse
        self.logger.debug("Applying binding_out {!r}, destination {!r}, relay_state {!r}".format(
            self.binding_out, self.destination, ticket.RelayState))
        http_args = self.IDP.apply_binding(self.binding_out, str(saml_response), self.destination,
                                           ticket.RelayState, response = True)
        #self.logger.debug("HTTPargs :\n{!s}".format(pprint.pformat(http_args)))

        # INFO-Log the SSO session id and the AL and destination
        self.logger.info("{!s}: response authn={!s}, dst={!s}".format(ticket.key,
                                                                      response_authn['class_ref'],
                                                                      self.destination))
        return eduid_idp.mischttp.create_html_response(self.binding_out, http_args, self.start_response, self.logger)

    def _validate_login_request(self, ticket):
        """
        Validate the validity of the SAML request we are going to answer with
        an assertion.

        Checks that the SP is known through metadata.

        Figures out how to respond to this request. Return a dictionary like

          {'destination': 'https://sp.example.org/saml2/acs/',
           'name_id_policy': <saml2.samlp.NameIDPolicy object>,
           'sp_entity_id': 'https://sp.example.org/saml2/metadata/',
           'binding': 'urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST',
           'in_response_to': 'id-4c45b079f571c57aef34aaaaac4295c9'
           }

        :param ticket: State for this request
        :return: pysaml2 response creation data

        :type ticket: SSOLoginData
        :rtype: dict
        """
        self.logger.debug("Validate login request :\n{!s}".format(str(ticket)))
        try:
            if not self._verify_request(ticket):
                raise eduid_idp.error.ServiceError(logger = self.logger)  # not reached
            resp_args = self.IDP.response_args(ticket.req_info.message)
        except UnknownPrincipal as excp:
            self.logger.info("{!s}: Unknown service provider: {!s}".format(ticket.key, excp))
            raise eduid_idp.error.BadRequest("Don't know the SP that referred you here", logger = self.logger)
        except UnsupportedBinding as excp:
            self.logger.info("{!s}: Unsupported SAML binding: {!s}".format(ticket.key, excp))
            raise eduid_idp.error.BadRequest("Don't know how to reply to the SP that referred you here",
                                             logger = self.logger)
        return resp_args

    def _verify_request(self, ticket):
        """
        Verify that a login request looks OK to this IdP, and figure out
        the outgoing binding and destination to use later.

        :param ticket: SSOLoginData instance
        :return: True on success
        Status is True if query is OK, and Response is either a Response() or None
        if Status is True.

        :type ticket: SSOLoginData
        :rtype: bool
        """
        assert isinstance(ticket, SSOLoginData)
        self.logger.debug("verify_request acting on previously parsed ticket.req_info {!s}".format(ticket.req_info))

        self.logger.debug("AuthnRequest {!r}".format(ticket.req_info.message))

        self.binding_out, self.destination = self.IDP.pick_binding("assertion_consumer_service",
                                                                   entity_id = ticket.req_info.message.issuer.text)

        self.logger.debug("Binding: %s, destination: %s" % (self.binding_out, self.destination))
        return True

    def _get_login_response_authn(self, ticket, user):
        """
        Figure out what AuthnContext to assert in the SAML response.

        The 'highest' Assurance-Level (AL) asserted is basically min(ID-proofing-AL, Authentication-AL).

        What AuthnContext is asserted is also heavily influenced by what the SP requested.

        Returns a pysaml2-style dictionary like

            {'authn_auth': u'https://idp.example.org/idp.xml',
             'authn_instant': 1391678156,
             'class_ref': 'http://www.swamid.se/policy/assurance/al1'
             }

        :param ticket: State for this request
        :param user: The user for whom the assertion will be made
        :return: Authn information (pysaml2 style)

        :type ticket: SSOLoginData
        :type user: IdPUser
        :rtype: dict
        """
        _authn = self.sso_session.get_authn_context(self.AUTHN_BROKER, logger=self.logger)
        self.logger.debug("User authenticated using Authn {!r}".format(_authn))
        if not _authn:
            # This could happen with SSO sessions refering to old authns during
            # reconfiguration of authns in the AUTHN_BROKER.
            raise eduid_idp.error.ServiceError('Unknown stored AuthnContext')

        # Decide what AuthnContext to assert based on the one requested in the request
        # and the authentication performed
        req_authn_context = self._get_requested_authn_context(ticket)

        authn_ctx = eduid_idp.assurance.canonical_req_authn_context(req_authn_context, self.logger)
        auth_info = self.AUTHN_BROKER.pick(authn_ctx)

        auth_levels = []
        if authn_ctx and len(auth_info):
            # `method' is just a no-op (true) value in the way eduid_idp uses the AuthnBroker -
            # filter out the `reference' values (canonical class_ref strings)
            levels_dict = {}
            # Turn references (e.g. 'eduid:level:1:100') into base levels (e.g. 'eduid:level:1')
            for (method, reference) in auth_info:
                this = self.AUTHN_BROKER[reference]
                levels_dict[this['class_ref']] = 1
            auth_levels = sorted(levels_dict.keys())
            self.logger.debug("Acceptable Authn levels (picked by AuthnBroker) : {!r}".format(auth_levels))

        response_authn = eduid_idp.assurance.response_authn(req_authn_context, _authn, auth_levels, self.logger)

        if not eduid_idp.assurance.permitted_authn(user, response_authn, self.logger):
            # XXX should return a login failure SAML response instead of an error in the IdP here.
            # The SP could potentially help the user much better than the IdP here.
            raise eduid_idp.error.Forbidden("Authn not permitted".format())

        try:
            self.logger.debug("Asserting AuthnContext {!r} (requested: {!r})".format(
                response_authn['class_ref'], req_authn_context.authn_context_class_ref[0].text))
        except AttributeError:
            self.logger.debug("Asserting AuthnContext {!r} (none requested)".format(response_authn['class_ref']))

        response_authn['authn_instant'] = self.sso_session.authn_timestamp

        return response_authn

    def _get_requested_authn_context(self, ticket):
        """
        Check if this SP has explicit Authn preferences in the metadata (some SPs are not
        capable of conveying this preference in the RequestedAuthnContext)

        :param ticket: State for this request
        :return: Requested Authn Context

        :type ticket: SSOLoginData
        :rtype: RequestedAuthnContext
        """
        req_authn_context = ticket.req_info.message.requested_authn_context
        attributes = self.IDP.metadata.entity_attributes(ticket.req_info.message.issuer.text)
        if "http://www.swamid.se/assurance-requirement" in attributes:
            # XXX don't just pick the first one from the list - choose the most applicable one somehow.
            new_authn = attributes["http://www.swamid.se/assurance-requirement"][0]
            requested = None
            if req_authn_context and req_authn_context.authn_context_class_ref:
                requested = req_authn_context.authn_context_class_ref[0].text
            self.logger.debug("Entity {!r} has AuthnCtx preferences in metadata. Overriding {!r} -> {!r}".format(
                ticket.req_info.message.issuer.text,
                requested,
                new_authn))
            req_authn_context = requested_authn_context(new_authn)
        return req_authn_context

    def redirect(self):
        """ This is the HTTP-redirect endpoint.

        :return: HTTP response
        :rtype: string
        """
        self.logger.debug("--- In SSO Redirect ---")
        _info = self.unpack_redirect()
        self.logger.debug("Unpacked redirect :\n{!s}".format(pprint.pformat(_info)))

        ticket = self.IDP.ticket.get_ticket(_info, binding=BINDING_HTTP_REDIRECT)
        return self._redirect_or_post(ticket)

    def post(self):
        """
        The HTTP-Post endpoint

        :return: HTTP response
        :rtype: string
        """
        self.logger.debug("--- In SSO POST ---")
        _info = self.unpack_either()

        ticket = self.IDP.ticket.get_ticket(_info, binding=BINDING_HTTP_POST)
        return self._redirect_or_post(ticket)

    def _redirect_or_post(self, ticket):
        """
        Commmon code for redirect() and post() endpoints.

        :param ticket: SSOLoginData instance
        :rtype: string
        """
        _force_authn = self._should_force_authn(ticket)
        if self.sso_session and not _force_authn:
            _ttl = self.config.sso_session_lifetime - self.sso_session.minutes_old
            self.logger.info("{!s}: proceeding sso_session={!s}, ttl={:}m".format(
                ticket.key, self.sso_session.public_id, _ttl))
            self.logger.debug("Continuing with Authn request {!r}".format(ticket.req_info))
            return self.perform_login(ticket)

        if not self.sso_session:
            self.logger.info("{!s}: authenticate ip={!s}".format(ticket.key, eduid_idp.mischttp.get_remote_ip()))
        if _force_authn and self.sso_session:
            self.logger.info("{!s}: force_authn sso_session={!s}".format(
                ticket.key, self.sso_session.public_id))

        req_authn_context = self._get_requested_authn_context(ticket)
        return self._not_authn(ticket, req_authn_context)

    def _should_force_authn(self, ticket):
        """
        Check if the IdP should force authentication of this request.

        Will check SAML ForceAuthn but avoid endless loops of forced authentications
        by looking if the SSO session says authentication was actually performed
        based on this SAML request.

        :type ticket: SSOLoginData
        :rtype: bool
        """
        if ticket.req_info.message.force_authn:
            if not self.sso_session:
                self.logger.debug("Force authn without session - ignoring")
                return True
            if ticket.req_info.message.id != self.sso_session.user_authn_request_id:
                self.logger.debug("Forcing authentication because of ForceAuthn with "
                                  "SSO session id {!r} != {!r}".format(
                                  self.sso_session.user_authn_request_id, ticket.req_info.message.id))
                return True
            self.logger.debug("Ignoring ForceAuthn, authn already performed for SAML request {!r}".format(
                ticket.req_info.message.id))
        return False

    def _not_authn(self, ticket, requested_authn_context):
        """
        Authenticate user. Either, the user hasn't logged in yet,
        or the service provider forces re-authentication.
        :param ticket: SSOLoginData instance
        :param requested_authn_context: saml2.samlp.RequestedAuthnContext instance
        :returns: HTTP response

        :type ticket: SSOLoginData
        :type requested_authn_context: saml2.samlp.RequestedAuthnContext
        :rtype: string
        """
        assert isinstance(ticket, SSOLoginData)
        redirect_uri = eduid_idp.mischttp.geturl(self.config, query = False)

        self.logger.debug("Do authentication, requested auth context : {!r}".format(requested_authn_context))

        authn_ctx = eduid_idp.assurance.canonical_req_authn_context(requested_authn_context, self.logger)
        auth_info = self.AUTHN_BROKER.pick(authn_ctx)

        if authn_ctx and len(auth_info):
            # `method' is just a no-op (true) value in the way eduid_idp uses the AuthnBroker -
            # filter out the `reference' values (canonical class_ref strings)
            auth_levels = [reference for (method, reference) in auth_info]
            self.logger.debug("Acceptable Authn levels (picked by AuthnBroker) : {!r}".format(auth_levels))

            return self._show_login_page(ticket, auth_levels, redirect_uri)

        raise eduid_idp.error.Unauthorized("No usable authentication method", logger = self.logger)

    def _show_login_page(self, ticket, auth_levels, redirect_uri):
        """
        Display the login form for all authentication methods.

        SSO._not_authn() chooses what authentication method to use based on
        requested AuthnContext and local configuration, and then calls this method
        to render the login page for this method.

        :param ticket: SSOLoginData instance
        :param auth_levels: list of strings with auth level names that would be valid for this request
        :param redirect_uri: string with URL to proceed to after authentication
        :return: HTTP response

        :rtype: string
        """
        assert isinstance(ticket, SSOLoginData)

        argv = {
            "action": "/verify",
            "username": "",
            "password": "",
            "key": ticket.key,
            "authn_reference": auth_levels[0],
            "redirect_uri": redirect_uri,
            "alert_msg": "",
            "sp_entity_id": "",
            "failcount": ticket.FailCount,
            "signup_link": self.config.signup_link,
            "password_reset_link": self.config.password_reset_link,
        }

        # Set alert msg if FailCount is greater than zero
        if ticket.FailCount:
            argv["alert_msg"] = "INCORRECT"  # "Incorrect username or password ({!s} attempts)".format(ticket.FailCount)

        try:
            argv["sp_entity_id"] = ticket.req_info.message.issuer.text
        except KeyError:
            pass

        self.logger.debug("Login page HTML substitution arguments :\n{!s}".format(pprint.pformat(argv)))

        # Look for login page in user preferred language
        content = eduid_idp.mischttp.localized_resource(self.start_response, 'login.html', self.config, self.logger)
        if not content:
            raise eduid_idp.error.NotFound()

        # apply simplistic HTML formatting to template in 'res'
        return content.format(**argv)

    def _make_scoped_eppn(self, attributes):
        """
        Add scope to unscopged eduPersonPrincipalName attributes before relasing them.

        What scope to add, if any, is currently controlled by the configuration parameter
        `default_eppn_scope'.

        :param attributes: Attributes of a user
        :return: New attributes

        :type attributes: dict
        :rtype: dict
        """
        eppn = attributes.get('eduPersonPrincipalName')
        if not eppn:
            return attributes
        if '@' not in eppn:
            scope = self.config.default_eppn_scope
            if scope:
                attributes['eduPersonPrincipalName'] = eppn + '@' + scope
        return attributes


# -----------------------------------------------------------------------------
# === Authentication ====
# -----------------------------------------------------------------------------


def do_verify(idp_app):
    """
    Perform authentication of user based on user provided credentials.

    What kind of authentication to perform was chosen by SSO._not_authn() when
    the login web page was to be rendered. It is passed to this function through
    an HTTP POST parameter (authn_reference).

    This function should not be thought of as a "was login successful" or not.
    It will figure out what authentication level to assert based on the authncontext
    requested, and the actual authentication that succeeded.

    :param idp_app: IdPApplication instance
    :return: Does not return
    :raise eduid_idp.mischttp.Redirect: On successful authentication, redirect to redirect_uri.

    :type idp_app: idp.IdPApplication
    """
    query = eduid_idp.mischttp.get_post()
    # extract password to keep it away from as much code as possible
    password = query['password']
    del query['password']
    _loggable = query.copy()
    if password:
        _loggable['password'] = '<redacted>'
    idp_app.logger.debug("do_verify parsed query :\n{!s}".format(pprint.pformat(_loggable)))

    _ticket = idp_app.IDP.ticket.get_ticket(query)

    user_authn = None
    authn_ref = query.get('authn_reference')
    if authn_ref:
        user_authn = eduid_idp.assurance.get_authn_context(idp_app.AUTHN_BROKER, authn_ref)
    if not user_authn:
        raise eduid_idp.error.Unauthorized("Bad authentication reference", logger = idp_app.logger)

    idp_app.logger.debug("Authenticating with {!r} (from authn_reference={!r})".format(
        user_authn['class_ref'], authn_ref))

    login_data = {'username': query.get('username'),
                  'password': password,
                  }
    del password  # keep out of any exception logs
    user = idp_app.authn.get_authn_user(login_data, user_authn, idp_app)

    if not user:
        _ticket.FailCount += 1
        idp_app.IDP.ticket.store_ticket(_ticket)
        idp_app.logger.debug("Unknown user or wrong password")
        _referer = eduid_idp.mischttp.get_request_header().get('Referer')
        if _referer:
            raise eduid_idp.mischttp.Redirect(str(_referer))
        raise eduid_idp.error.Unauthorized("Login incorrect", logger = idp_app.logger)

    # Create SSO session
    idp_app.logger.debug("User {!r} authenticated OK using {!r}".format(user, user_authn['class_ref']))
    _sso_session = SSOSession(user_id = user.identity['_id'],
                              authn_ref = authn_ref,
                              authn_class_ref = user_authn['class_ref'],
                              authn_request_id = _ticket.req_info.message.id,
                              )

    # This session contains information about the fact that the user was authenticated. It is
    # used to avoid requiring subsequent authentication for the same user during a limited
    # period of time, by storing the session-id in a browser cookie.
    _session_id = idp_app.IDP.cache.add_session(user.identity['_id'], _sso_session.to_dict())
    eduid_idp.mischttp.set_cookie("idpauthn", idp_app.config.sso_session_lifetime, "/", idp_app.logger, _session_id)
    # knowledge of the _session_id enables impersonation, so get rid of it as soon as possible
    del _session_id

    # INFO-Log the request id (sha1 of SAMLrequest) and the sso_session
    idp_app.logger.info("{!s}: login sso_session={!s}, authn={!s}, user={!s}".format(
        query['key'], _sso_session.public_id,
        _sso_session.user_authn_class_ref,
        _sso_session.user_id))

    # Now that an SSO session has been created, redirect the users browser back to
    # the main entry point of the IdP (the 'redirect_uri'). The ticket reference `key'
    # is passed as an URL parameter instead of the SAMLRequest.
    lox = query["redirect_uri"] + '?key=' + query['key']
    idp_app.logger.debug("Redirect => %s" % lox)
    raise eduid_idp.mischttp.Redirect(lox)


# ----------------------------------------------------------------------------
