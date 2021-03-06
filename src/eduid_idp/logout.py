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
Code handling Single Log Out requests.
"""
import pprint
from hashlib import sha1

import eduid_idp
from eduid_idp.service import Service

from saml2 import BINDING_HTTP_REDIRECT
from saml2 import BINDING_HTTP_POST
from saml2 import BINDING_SOAP
from saml2.s_utils import exception_trace, error_status_factory
import saml2.samlp
import saml2.request

# -----------------------------------------------------------------------------
# === Single log out ===
# -----------------------------------------------------------------------------


class SLO(Service):
    """
    Single Log Out service.
    """

    def redirect(self):
        """ Expects a HTTP-redirect request """

        _dict = self.unpack_redirect()
        return self.perform_logout(_dict, BINDING_HTTP_REDIRECT)

    def post(self):
        """ Expects a HTTP-POST request """

        _dict = self.unpack_post()
        return self.perform_logout(_dict, BINDING_HTTP_POST)

    def soap(self):
        """
        Single log out using HTTP_SOAP binding
        """
        _dict = self.unpack_soap()
        return self.perform_logout(_dict, BINDING_SOAP)

    def unpack_soap(self):
        """
        Turn a SOAP request into the common format of a dict.

        :return: dict with 'SAMLRequest' and 'RelayState' items
        """
        query = eduid_idp.mischttp.get_request_body()
        return {'SAMLRequest': query,
                'RelayState': '',
                }

    def perform_logout(self, info, binding):
        """
        Perform logout. Means remove SSO session from IdP list, and a best
        effort to contact all SPs that have received assertions using this
        SSO session and letting them know the user has been logged out.

        :param info: Dict with SAMLRequest and possibly RelayState
        :param binding: SAML2 binding as string
        :return: SAML StatusCode

        :type info: dict
        :type binding: string
        :rtype: string
        """
        self.logger.debug("--- Single Log Out Service ---")
        if not info:
            raise eduid_idp.error.BadRequest('Error parsing request or no request', logger = self.logger)

        request = info["SAMLRequest"]
        req_key = _get_request_key(request)

        try:
            req_info = self.IDP.parse_logout_request(request, binding)
            assert isinstance(req_info, saml2.request.LogoutRequest)
            self.logger.debug("Parsed Logout request ({!s}):\n{!s}".format(binding, req_info.message))
        except Exception as exc:
            self.logger.debug("_perform_logout {!s}:\n{!s}".format(binding, pprint.pformat(info)))
            self.logger.error("Bad request parsing logout request : {!r}".format(exc))
            self.logger.debug("Exception parsing logout request :\n{!s}".format(exception_trace(exc)))
            raise eduid_idp.error.BadRequest("Failed parsing logout request", logger = self.logger)

        req_info.binding = binding
        if 'RelayState' in info:
            req_info.relay_state = info['RelayState']

        # look for the subject
        subject = req_info.subject_id()
        if subject is not None:
            self.logger.debug("Logout subject: {!s}".format(subject.text.strip()))
        # XXX should verify issuer (a.k.a. sender()) somehow perhaps
        self.logger.debug("Logout request sender : {!s}".format(req_info.sender()))

        _name_id = req_info.message.name_id
        _session_id = eduid_idp.mischttp.read_cookie(self.logger)
        _username = None
        if _session_id:
            # If the binding is REDIRECT, we can get the SSO session to log out from the
            # client idpauthn cookie
            session_ids = [_session_id]
        else:
            # For SOAP binding, no cookie is sent - only NameID. Have to figure out
            # the user based on NameID and then destroy *all* the users SSO sessions
            # unfortunately.
            _username = self.IDP.ident.find_local_id(_name_id)
            self.logger.debug("Logout message name_id: {!r} found username {!r}".format(
                _name_id, _username))
            session_ids = self.IDP.cache.get_sessions_for_user(_username)

        self.logger.debug("Logout resources: name_id {!r} username {!r}, session_ids {!r}".format(
            _name_id, _username, session_ids))

        if session_ids:
            status_code = self._logout_session_ids(session_ids, req_key)
        else:
            # No specific SSO session(s) were found, we have no choice but to logout ALL
            # the sessions for this NameID.
            status_code = self._logout_name_id(_name_id, req_key)

        self.logger.debug("Logout of sessions {!r} / NameID {!r} result : {!r}".format(
            session_ids, _name_id, status_code))
        return self._logout_response(req_info, status_code, req_key)

    def _logout_session_ids(self, session_ids, req_key):
        """
        Terminate one or more specific SSO sessions.

        :param session_ids: List of db keys in SSO session database
        :param req_key: Logging id of request
        :return: SAML StatusCode
        :rtype: string
        """
        fail = 0
        for this in session_ids:
            self.logger.debug("Logging out SSO session with key: {!s}".format(this))
            try:
                _data = self.IDP.cache.get_session(this)
                if not _data:
                    raise KeyError('Session not found')
                _sso = eduid_idp.sso_session.from_dict(_data)
                res = self.IDP.cache.remove_session(this)
                self.logger.info("{!s}: logout sso_session={!r}, age={!r}m, result={!r}".format(
                    req_key, _sso.public_id, _sso.minutes_old, bool(res)))
            except KeyError:
                self.logger.info("{!s}: logout sso_key={!r}, result=not_found".format(req_key, this))
                res = 0
            if not res:
                fail += 1
        if fail:
            if fail == len(session_ids):
                return saml2.samlp.STATUS_RESPONDER
            return saml2.samlp.STATUS_PARTIAL_LOGOUT
        return saml2.samlp.STATUS_SUCCESS

    def _logout_name_id(self, name_id, req_key):
        """
        Terminate ALL SSO sessions found using this NameID.

        This is not as nice as _logout_session_ids(), as it would log a user
        out of sessions across multiple devices - probably not the expected thing
        to happen from a user perspective when clicking Logout on their phone.

        :param name_id: NameID from LogoutRequest
        :param req_key: Logging id of request
        :return: SAML StatusCode
        :rtype: string
        """
        if not name_id:
            self.logger.debug("No NameID provided for logout")
            return saml2.samlp.STATUS_UNKNOWN_PRINCIPAL
        try:
            # remove the authentication
            # XXX would be useful if remove_authn_statements() returned how many statements it actually removed
            self.IDP.session_db.remove_authn_statements(name_id)
            self.logger.info("{!s}: logout name_id={!r}".format(req_key, name_id))
        except KeyError as exc:
            self.logger.error("ServiceError removing authn : %s" % exc)
            raise eduid_idp.error.ServiceError(logger = self.logger)
        return saml2.samlp.STATUS_SUCCESS

    def _logout_response(self, req_info, status_code, req_key, sign_response=True):
        """
        Create logout response.

        :param req_info: Logout request
        :param status_code: logout result (e.g. 'urn:oasis:names:tc:SAML:2.0:status:Success')
        :param req_key: SAML request id
        :param sign_response: cryptographically sign response or not
        :return: HTML response

        :type req_info: saml2.request.LogoutRequest
        :type status_code: string
        :type req_key: string
        :type sign_response: bool
        :rtype: string
        """
        self.logger.debug("LOGOUT of '{!s}' by '{!s}', success={!r}".format(req_info.subject_id(),
                                                                            req_info.sender(),
                                                                            status_code))
        if req_info.binding != BINDING_SOAP:
            bindings = [BINDING_HTTP_REDIRECT, BINDING_HTTP_POST]
            binding, destination = self.IDP.pick_binding("single_logout_service", bindings,
                                                         entity_id = req_info.sender())
            bindings = [binding]
        else:
            bindings = [BINDING_SOAP]
            destination = ""

        status = None  # None == success in create_logout_response()
        if status_code != saml2.samlp.STATUS_SUCCESS:
            status = error_status_factory((status_code, "Logout failed"))
            self.logger.debug("Created 'logout failed' status based on {!r} : {!r}".format(status_code, status))

        issuer = self.IDP._issuer(self.IDP.config.entityid)
        response = self.IDP.create_logout_response(req_info.message, bindings, status, sign = sign_response,
                                                   issuer = issuer)
        # Only perform expensive parse/pretty-print if debugging
        if self.config.debug:
            xmlstr = eduid_idp.util.maybe_xml_to_string(response, logger=self.logger)
            self.logger.debug("Logout SAMLResponse :\n\n{!s}\n\n".format(xmlstr))

        ht_args = self.IDP.apply_binding(bindings[0], str(response), destination, req_info.relay_state,
                                         response = True)
        # self.logger.debug("Apply bindings result :\n{!s}\n\n".format(pprint.pformat(ht_args)))

        # Delete the SSO session cookie in the browser
        eduid_idp.mischttp.delete_cookie('idpauthn', self.logger, self.config)

        # INFO-Log the SAML request ID, result of logout and destination
        self.logger.info("{!s}: logout status={!r}, dst={!s}".format(req_key, status_code, destination))

        # XXX old code checked 'if req_info.binding == BINDING_HTTP_REDIRECT:', but it looks like
        # it would be more correct to look at bindings[0] here, since `bindings' is what was used
        # with create_logout_response() and apply_binding().
        if req_info.binding != bindings[0]:
            self.logger.debug("Creating response with binding {!r] instead of {!r} used before".format(
                bindings[0], req_info.binding))
        return eduid_idp.mischttp.create_html_response(bindings[0], ht_args, self.start_response, self.logger)


def _get_request_key(request):
    """
    Generate a unique identifier for this request looking like the ticket.key in login.

    :param request: SAMLRequest as string
    :return: unique id

    :type request: string
    :rtype: string
    """
    return sha1(request).hexdigest()
