#
# Copyright (c) 2013 NORDUnet A/S
# Copyright 2012 Roland Hedberg. All rights reserved.
# All rights reserved.
#
# See the file eduid-IdP/LICENSE.txt for license statement.
#
# Author : Fredrik Thulin <fredrik@thulin.net>
#          Roland Hedberg
#

"""
Common code for SSO login/logout requests.
"""

import pprint

from saml2 import BINDING_HTTP_ARTIFACT

import eduid_idp.mischttp
from eduid_idp.mischttp import Response, Redirect


class Service(object):
    """
    Base service class. Common code for SSO and SLO classes.

    :param environ: environ dict() (see eduid_idp.idp._request_environment())
    :param start_response: WSGI-like start_response function pointer
    :param idp_app: IdPApplication instance
    """

    def __init__(self, environ, start_response, idp_app):
        self.environ = environ
        idp_app.logger.debug("ENVIRON:\n{!s}".format(pprint.pformat(environ)))
        self.start_response = start_response
        self.logger = idp_app.logger
        self.IDP = idp_app.IDP
        self.AUTHN_BROKER = idp_app.AUTHN_BROKER
        self.config = idp_app.config
        self.user = environ['idp.user']

    def unpack_redirect(self):
        return eduid_idp.mischttp.parse_query_string()

    def unpack_post(self):
        #info = parse_qs(get_post(self.environ))
        info = eduid_idp.mischttp.get_post()
        self.logger.debug("unpack_post:: %s" % info)
        try:
            return dict([(k, v) for k, v in info.items()])
        except AttributeError:
            return None

    def unpack_either(self):
        if self.environ["REQUEST_METHOD"] == "GET":
            _dict = self.unpack_redirect()
        elif self.environ["REQUEST_METHOD"] == "POST":
            _dict = self.unpack_post()
        else:
            _dict = None
        self.logger.debug("_dict: %s" % _dict)
        return _dict

    def response(self, binding, http_args):
        if binding == BINDING_HTTP_ARTIFACT:
            # XXX This URL extraction code is untested in practice, but it appears
            # the should be HTTP headers in http_args['headers']
            urls = [v for (k, v) in http_args['headers'] if k == 'Location']
            resp = Redirect(urls)
        else:
            resp = Response(http_args["data"], headers = http_args["headers"])
        return resp(self.environ, self.start_response)

    def redirect(self):
        """ Expects a HTTP-redirect request """
        raise NotImplementedError('Subclass should implement function "redirect"')

    def post(self):
        """ Expects a HTTP-POST request """
        raise NotImplementedError('Subclass should implement function "post"')
