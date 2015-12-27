import json
from collections import namedtuple
from http.client import HTTPConnection, HTTPSConnection
from base64 import b64encode
from enum import Enum


class CouchDBException(Exception):
    def __init__(self, response):
        self._response = response

    @property
    def status(self):
        return self._response.status

    @property
    def reason(self):
        return self._response.reason

    @property
    def body(self):
        return self._response.body

    @property
    def content_type(self):
        return self._response.content_type

    @property
    def is_json(self):
        return self._response.is_json

    def __str__(self):
        if self.is_json:
            return '{self.status}: {self.reason} - {self.body.reason}'.format(self=self)
        else:
            return '{self.status}: {self.reason}'.format(self=self)


class CouchDB:
    class DatabaseType(Enum):
        CouchDB = 1
        AvanceDB = 2
        PouchDB = 3
        Cloudant = 4

    class Response:
        def __init__(self, response, body=None, content_type=None):
            self._response = response
            self._content_type = content_type if content_type is not None else response.getheader('content-type')
            self._body = body

        @property
        def status(self):
            return self._response.status

        @property
        def reason(self):
            return self._response.reason

        @property
        def body(self):
            return self._body

        @property
        def content_type(self):
            return self._content_type

        @property
        def is_json(self):
            return self._content_type.find('application/json') == 0

    _auth = None
    _auth_active = False
    _signature = None

    def __init__(self, host, port, secure, get_credentials=None):
        self._host = host
        self._port = port
        self._secure = secure
        self._get_credentials = get_credentials
        self._conn = HTTPSConnection(host, port) if secure else HTTPConnection(host, port)

    @property
    def db_type(self):
        db_type = CouchDB.DatabaseType.CouchDB
        signature = self.get_signature()
        if getattr(signature, 'express_pouchdb', None):
            db_type = CouchDB.DatabaseType.PouchDB
        elif getattr(signature, 'avancedb', None):
            db_type = CouchDB.DatabaseType.AvanceDB
        elif getattr(signature, 'cloudant_build', None):
            db_type = CouchDB.DatabaseType.Cloudant
        return db_type

    @property
    def auth(self):
        return self._auth

    def get_url(self):
        url = 'https' if self._secure else 'http'
        url += '://' + self._host + ':' + self._port + '/'
        return url

    def get_signature(self):
        if not self._signature:
            response = self._make_request('/')
            if response.status != 200 or not response.is_json:
                raise CouchDBException(response)
            self._signature = response.body
        return self._signature

    def get_session(self):
        response = self._make_request('/_session')
        if response.status != 200 or not response.is_json:
            raise CouchDBException(response)
        return response.body

    def create_database(self, name):
        response = self._make_request('/' + name, 'PUT')
        if response.status != 201 or not response.is_json:
            raise CouchDBException(response)

    def get_database(self, name):
        response = self._make_request('/' + name)
        if response.status != 200 or not response.is_json:
            raise CouchDBException(response)
        return response.body

    def delete_database(self, name):
        response = self._make_request('/' + name, 'DELETE')
        if response.status != 200 or not response.is_json:
            raise CouchDBException(response)

    def get_databases(self):
        response = self._make_request('/_all_dbs')
        if response.status != 200 or not response.is_json:
            raise CouchDBException(response)
        return response.body

    def get_active_tasks(self, task_type=None):
        response = self._make_request('/_active_tasks')
        if response.status != 200 or not response.is_json:
            raise CouchDBException(response)

        tasks = response.body
        if task_type:
            tasks = [task for task in tasks if task.type == task_type]

        return tasks

    def create_replication(self, source, target, create_target=False, continuous=False):
        job = {'source': source, 'target': target, 'create_target': create_target, 'continuous': continuous}

        if self._auth is not None:
            session = self.get_session()
            user_ctx = session.userCtx
            job['user_ctx'] = {'name': user_ctx.name, 'roles': user_ctx.roles}

        job_json = json.dumps(job)
        response = self._make_request('/_replicator', 'POST', job_json, 'application/json')
        if response.status != 201 or not response.is_json:
            raise CouchDBException(response)
        return response.body

    def compact_database(self, name):
        response = self._make_request('/' + name + '/_compact', 'POST', None, 'application/json')
        if response.status != 202 or not response.is_json:
            raise CouchDBException(response)

    def _make_request(self, uri, method='GET', body=None, content_type=None):
        headers = {}
        if self._auth:
            headers['Authorization'] = 'Basic ' + self._auth

        if (method == 'PUT' or method == 'POST') and content_type is not None:
            headers['Content-Type'] = content_type

        self._conn.request(method, uri, body, headers)

        response = self._conn.getresponse()
        response_body = response.readall()

        if response.status == 401 and callable(self._get_credentials) and not self._auth_active:
            try:
                self._auth_active = True
                server_url = self.get_url()
                creds = self._get_credentials(server_url)
                self._auth_active = False

                if creds:
                    auth = creds.username + ':' + creds.password
                    auth = auth.encode()
                    self._auth = b64encode(auth).decode("ascii")
                    return self._make_request(uri, method, body, content_type)
            finally:
                self._auth_active = False

        response_content_type = response.getheader('content-type')
        if response_content_type.find('utf-8') >= 0:
            response_body = response_body.decode('utf-8')
        else:
            response_body = response_body.decode('ascii')

        if response_content_type.find('text/plain') == 0 and \
                len(response_body) > 0 and \
                (response_body[0] == '{' or response_body[0] == '['):
            response_content_type = response_content_type.replace('text/plain', 'application/json')

        if response_content_type.find('application/json') == 0:
            response_body = json.loads(
                response_body,
                object_hook=lambda o: namedtuple('Struct', CouchDB._validate_keys(o.keys()))(*o.values()))

        return CouchDB.Response(response, response_body, response_content_type)

    @staticmethod
    def _validate_keys(keys):
        new_keys = []
        for key in keys:
            new_keys.append(key.replace('-', '_'))
        return new_keys
