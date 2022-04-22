__author__ = 'fhcarter'
__copyright__ = "Copyright 2022, Vantiq, Inc."
__license__ = "MIT License"
__email__ = "support@vantiq.com"

import asyncio
from datetime import datetime
import os
import traceback
from typing import Union

import aiofiles
import pytest

from vantiqsdk import Vantiq, VantiqException, VantiqResources, VantiqResponse

_server_url: Union[str, None] = None
_access_token: Union[str, None] = None
_username: Union[str, None] = None
_password: Union[str, None] = None


@pytest.fixture(autouse=True)
def pytest_sessionstart():
    global _server_url
    global _access_token
    global _username
    global _password
    _server_url = os.getenv('VANTIQ_URL')
    _access_token = os.getenv('VANTIQ_ACCESS_TOKEN')
    _username = os.getenv('VANTIQ_USERNAME')
    _password = os.getenv('VANTIQ_PASSWORD')


TEST_TOPIC = '/test/pythonsdk/topic'
TEST_RELIABLE_TOPIC = '/test/pythonsdk/reliabletopic'
TEST_PROCEDURE = 'pythonsdk.echo'
TEST_TYPE = 'TestType'


class TestLiveConnection:

    async def setup_test_env(self, client: Vantiq):
        try:
            proc_def = {'ruleText':
f'''PROCEDURE {TEST_PROCEDURE}(arg1, arg2)

return {{
    arg1: arg1,
    arg2: arg2,
    namespace: Context.namespace()
}}'''}
            vr = await client.insert('system.procedures', proc_def)
            assert vr.is_success
            assert vr.errors is None or vr.errors == []

            test_type_def = {"name": TEST_TYPE,
                             "properties": {
                                "id": {"type": "String"},
                                "ts": {"type": "DateTime"},
                                "x":  {"type": "Real"},
                                "k":  {"type": "Integer"},
                                "o":  {"type": "Object"}
                              },
                             "naturalKey": ["id"],
                             "indexes": [{"keys": ["id"], "options": {"unique": True}}]
                             }
            vr = await client.insert(VantiqResources.TYPES, test_type_def)
            assert vr.is_success
            vr = await client.delete(test_type_def['name'], None)
            assert vr.is_success

            rule = {'ruleText':
f"""RULE onTestPublish

WHEN PUBLISH OCCURS ON "{TEST_TOPIC}" AS event

log.error("Event {{}}", [event.newValue])

INSERT INTO {TEST_TYPE}(event.newValue)
Event.ack()"""}
            vr = await client.insert(VantiqResources.RULES, rule)
            assert vr.is_success
        except Exception:
            print('Unable to setup environment:', traceback.format_exc())

    @pytest.fixture(autouse=True)
    def _setup(self):
        """This method replaces/augments the usual __init__(self).  __init__(self) is not supported by pytest.
        Its primary purpose here is to 'declare' (via assignment) the instance variables.
        """
        self._acquired_doc = None
        self._doc_is_from = None
        self.callback_count = 0
        self.callbacks = []
        self.message_checker = None
        self.last_message: Union[dict, None] = None

    def dump_errors(self, tag: str, vr: VantiqResponse):
        if not vr.is_success:
            for err in vr.errors:
                print('{0}: {1}'.format(tag, err))
        else:
            print('{0} is OK'.format(tag))

    def process_chunk(self, doc_url: str, length: int, chunk: bytes) -> None:
        assert length > 0
        assert len(chunk) == length
        if self._doc_is_from is None:
            self._doc_is_from = doc_url
            assert len(self._acquired_doc) == 0  # Failure here is probably a test issue rather than product...
        else:
            assert self._doc_is_from == doc_url
        self._acquired_doc.extend(chunk)

    async def check_download(self, client: Vantiq, content_url: str, expected_content: Union[bytes, bytearray]):
        # Check download direct
        vr = await client.download(content_url)
        assert isinstance(vr, VantiqResponse)
        assert vr.is_success
        contents = await vr.body.read()
        assert contents is not None
        assert contents == expected_content

    def check_callback_type_insert(self, what: str, msg: dict):
        assert what in ['connect', 'message']
        if what == 'message':
            assert msg['status'] < 400
            assert msg['body']['path'].startswith('/types/TestType/insert')
            assert msg['body']['value']['id'] == 'test_insert'

    def check_callback_topic_publish(self, what: str, msg: dict):
        assert what in ['connect', 'message']
        if what == 'message':
            assert msg['status'] < 400
            assert msg['body']['path'] == '/topics/test/pythonsdk/topic/publish'
            assert msg['body']['value'] == {'foo': 'bar'}

    def check_callback_topic_publish_saveit(self, what: str, msg: dict):
        assert what in ['connect', 'message']
        if what == 'message':
            assert msg['status'] < 400
        self.last_message = msg

    async def subscriber_callback(self, what: str, details: dict) -> None:
        print('Subscriber got a callback -- what: {0}, details: {1}'.format(what, details))
        self.callback_count += 1
        self.callbacks.append(what)
        if self.message_checker is not None:
            self.message_checker(what, details)

    async def check_subscription_ops(self, client: Vantiq, prestart_transport: bool):
        if prestart_transport:
            await client.start_subscriber_transport()

        await client.delete(TEST_TYPE, {})
        vr = await client.subscribe(VantiqResources.TOPICS, TEST_TOPIC, None, self.subscriber_callback, {})
        assert isinstance(vr, VantiqResponse)
        self.dump_errors('Subscription Error', vr)
        assert vr.is_success
        orig_count = self.callback_count
        await asyncio.sleep(0.5)
        self.message_checker = self.check_callback_topic_publish

        vr = await client.publish(VantiqResources.TOPICS, TEST_TOPIC, {'foo': 'bar'})
        assert isinstance(vr, VantiqResponse)
        assert vr.is_success

        # Now, we should see that our callback was called after a little while.

        while self.callback_count < orig_count + 2:
            await asyncio.sleep(0.1)
        assert self.callbacks == ['connect', 'message']

        self.callbacks = []

        vr = await client.subscribe(VantiqResources.TYPES, TEST_TYPE, 'insert', self.subscriber_callback, {})
        assert isinstance(vr, VantiqResponse)

        self.dump_errors('Subscription Error', vr)
        assert vr.is_success
        orig_count = self.callback_count
        await asyncio.sleep(0.5)
        self.message_checker = self.check_callback_type_insert

        vr = await client.insert(TEST_TYPE, {'id': 'test_insert'})
        assert isinstance(vr, VantiqResponse)
        assert vr.is_success

        # Now, we should see that our callback was called after a little while.

        while self.callback_count < orig_count + 2:
            await asyncio.sleep(0.1)
        assert self.callbacks == ['connect', 'message']

        new_topic = {"name": TEST_RELIABLE_TOPIC,
                     "description": "topic description",
                     "isReliable": True,
                     "redeliveryFrequency": 5,
                     "redeliveryTTL": 100}

        vr = await client.insert("system.topics", new_topic)
        self.dump_errors('Upsert to create reliable topic', vr)
        assert vr.is_success
        params = {'persistent': True}

        self.message_checker = self.check_callback_topic_publish_saveit

        vr = await client.subscribe(VantiqResources.TOPICS, TEST_RELIABLE_TOPIC, None, self.subscriber_callback, params)
        self.dump_errors('Subscribe to reliable', vr)
        assert vr.is_success
        while self.last_message is None:
            await asyncio.sleep(0.1)

        last = self.last_message

        print('Callback message:', last)
        assert 'headers' in last
        # noinspection PyUnresolvedReferences
        assert 'X-Request-Id' in last['headers']
        assert 'body' in last
        # noinspection PyUnresolvedReferences
        assert 'name' in last['body']
        # noinspection PyUnresolvedReferences
        subscription_id = last['body']['name']  #
        # noinspection PyUnresolvedReferences
        request_id = last['headers']['X-Request-Id']  # Looks like a topic path
        assert isinstance(subscription_id, str)
        assert isinstance(request_id, str)

        # Synchronously publish to the topic
        now = datetime.now()
        dt = now.strftime('%Y-%m-%dT%H:%M:%SZ')
        body = {"ts": dt, 'id': f'ACK-{dt}'}

        self.last_message = None
        vr = await client.publish(VantiqResources.TOPICS, TEST_RELIABLE_TOPIC, body)
        assert vr.is_success
        while self.last_message is None:
            await asyncio.sleep(0.1)

        last = self.last_message
        assert 'body' in last
        # noinspection PyUnresolvedReferences
        assert last['headers']['X-Request-Id'] == '/topics' + TEST_RELIABLE_TOPIC
        # noinspection PyUnresolvedReferences
        assert last['body']['path'] == '/topics' + TEST_RELIABLE_TOPIC + '/publish'

        assert isinstance(last, dict)
        await client.ack(request_id, subscription_id, last['body'])

        print('request_id: {0}, sub_id: {1}, partition_id: {2}, seq_id: {3}'.format(request_id, subscription_id,
                                                                                    last['body']['partitionId'],
                                                                                    last['body']['sequenceId']))

        where = {"subscriptionId": subscription_id}
        vr = await client.select("ArsEventAcknowledgement", None, where)
        self.dump_errors('Select of event acks', vr)

        assert vr.is_success
        body = vr.body
        assert isinstance(body, list)
        for row in body:
            print('Event act:', row)
        assert len(body) == 0
        assert vr.is_success

    async def check_documentesque_operation(self, client: Vantiq, skip_pretest_cleanup: bool = False) -> None:
        # First, clean the environment
        if not skip_pretest_cleanup:
            await client.delete(VantiqResources.DOCUMENTS, None)

        # Check doc where contents are directly inserted
        file_content = 'abcdefgh' * 1000
        doc = {'name': 'test_doc', 'fileType': 'text/plain', 'content': file_content}
        vr = await client.insert(VantiqResources.DOCUMENTS, doc)
        assert isinstance(vr, VantiqResponse)
        assert vr.is_success
        res = vr.body
        assert res is not None
        assert isinstance(res, dict)
        assert res['name'] == 'test_doc'
        vr = await client.select_one(VantiqResources.DOCUMENTS, 'test_doc')
        assert isinstance(vr, VantiqResponse)
        self.dump_errors('Insert doc', vr)
        assert vr.is_success
        doc = vr.body
        assert doc is not None
        assert isinstance(doc, dict)
        assert doc['name'] == 'test_doc'
        assert doc['fileType'] == 'text/plain'
        assert doc['content'] == '/docs/' + doc['name']
        assert doc['contentSize'] == len(file_content)

        # Test download capabilities.  check will handle both direct & via callback
        await self.check_download(client, doc['content'], bytes(file_content, 'utf-8'))

        # Now, test ability to upload a document (as opposed to insert as above)
        # Check for creation as well as content existence & fetchability
        file_content = '1234567890' * 2000
        vr = await client.upload(VantiqResources.DOCUMENTS, 'text/plain',
                                 filename='test_doc_upload', inmem=file_content)
        assert isinstance(vr, VantiqResponse)
        assert vr.is_success
        res = vr.body
        assert res is not None
        assert isinstance(res, dict)
        print('Document uploaded: ', res)
        vr = await client.select_one(VantiqResources.DOCUMENTS, 'test_doc_upload')
        print('Document fetch post insert', vr)
        assert isinstance(vr, VantiqResponse)
        assert vr.is_success
        doc = vr.body
        assert doc['contentSize'] == len(file_content)

        await self.check_download(client, doc['content'], bytes(file_content, 'utf-8'))

        # Now, check the same thing with a file located at a root location
        # This is of interest since Vantiq doesn't permit document names that start
        # with '/'.  This also checks to ensure that path names work as document names
        # (as they should), but the mechanism used can url-encode when not expected.
        filename = '/tmp/test_file'
        filename_sans_leading_slash = filename[len('/'):]  # filename.removeprefix('/')
        file_content = 'ZYXWVUTSRQPONMLKJIHGFEDCBA' * 250
        async with aiofiles.open(filename, mode='wb') as f:
            await f.write(bytes(file_content, 'utf-8'))

        vr = await client.upload(VantiqResources.DOCUMENTS, 'text/plain', filename=filename,
                                 doc_name=filename_sans_leading_slash)
        assert isinstance(vr, VantiqResponse)
        assert vr.is_success
        doc = vr.body
        assert isinstance(doc, dict)
        # Verify document set is as we expect
        vr = await client.select(VantiqResources.DOCUMENTS)
        assert isinstance(vr, VantiqResponse)
        assert vr.is_success
        docs = vr.body
        assert isinstance(docs, list)
        for adoc in docs:
            assert isinstance(adoc, dict)
            assert adoc['content'].endswith(adoc['name'])
            assert adoc['content'].startswith('/docs/')
        if not skip_pretest_cleanup:
            assert len(docs) == 3

        vr = await client.count(VantiqResources.DOCUMENTS, None)
        assert vr.count is not None
        assert vr.count == len(docs)

        vr = await client.select_one(VantiqResources.DOCUMENTS, filename_sans_leading_slash)
        assert isinstance(vr, VantiqResponse)
        assert vr.is_success
        doc = vr.body
        assert isinstance(doc, dict)
        assert doc['name'] == filename_sans_leading_slash
        assert doc['contentSize'] == len(file_content)

        await self.check_download(client, doc['content'], bytes(file_content, 'utf-8'))

    async def check_crud_operations(self, client: Vantiq, skip_pretest_cleanup: bool = False):
        vr = await client.select(VantiqResources.TYPES)
        found_clusters = False
        assert isinstance(vr, VantiqResponse)
        assert vr.is_success
        rows = vr.body
        assert isinstance(rows, list)
        for row in rows:
            assert 'name' in row
            assert 'ars_namespace' in row
            if 'resourceName' in row:
                if row['resourceName'] == VantiqResources.K8S_CLUSTERS:
                    found_clusters = True
        assert found_clusters
        vr = await client.select_one(VantiqResources.TYPES, 'ArsNamespace')
        assert isinstance(vr, VantiqResponse)
        assert vr.is_success
        row = vr.body
        assert isinstance(row, dict)
        assert 'resourceName' in row
        assert 'ars_namespace' in row
        assert 'name' in row
        assert row['resourceName'] == VantiqResources.unqualified_name(VantiqResources.NAMESPACES)

        try:
            vr = await client.select(VantiqResources.TYPES, ["name", "resourceName"],
                                     {"$or": [{"name": "ArsType"}, {'name': 'ArsTensorFlowModel'}]},
                                     {"name": -1}, 1, {'required': 'true'})
            assert isinstance(vr, VantiqResponse)
            assert vr.is_success
            # Here, we'd get two rows, but we're sorting them in reverse & limiting the results to 1.
            # Checking that myriad parameters works as expected...
            rows = vr.body
            assert isinstance(rows, list)
            assert rows is not None
            assert len(rows) == 1
            assert 'resourceName' in rows[0]
            assert rows[0]['resourceName'] == VantiqResources.unqualified_name(VantiqResources.TYPES)

            vr = await client.select(VantiqResources.TYPES)
            assert isinstance(vr, VantiqResponse)
            assert vr.is_success
            rows = vr.body
            assert rows is not None
            assert isinstance(rows, list)
            assert len(rows) > 0

            vr = await client.count(VantiqResources.TYPES, None)
            assert vr.is_success
            assert vr.count is not None
            assert vr.count == len(rows)

            coroutine = client.count(VantiqResources.TYPES,
                                     {'resourceName': VantiqResources.unqualified_name(VantiqResources.TYPES)})
            assert coroutine is not None
            vr = await coroutine
            assert vr is not None
            assert vr.is_success
            assert vr.count == 1
        except VantiqException as ve:
            traceback.print_exc()
            assert ve is None

        # Initialize tests

        if not skip_pretest_cleanup:
            vr = await client.delete(VantiqResources.K8S_CLUSTERS, None)
            assert isinstance(vr, VantiqResponse)
            assert vr.is_success
            res = vr.body
            assert isinstance(res, dict)
            assert len(res) == 0

        test_cluster_name = 'pythonTestCluster'
        vr = await client.insert(VantiqResources.K8S_CLUSTERS, {'name': test_cluster_name})
        assert isinstance(vr, VantiqResponse)
        assert vr.is_success
        res = vr.body
        assert res is not None
        assert isinstance(res, dict)
        assert 'name' in res
        assert 'ingressDefaultNode' in res
        assert '_id' in res
        assert res['name'] == test_cluster_name
        assert res['ingressDefaultNode'] == f'vantiq-{test_cluster_name}-node'.lower()
        assert res['_id'] is not None

        vr = await client.select_one(VantiqResources.K8S_CLUSTERS, test_cluster_name)
        assert isinstance(vr, VantiqResponse)
        assert vr.is_success
        row = vr.body
        assert row is not None
        assert isinstance(row, dict)
        new_node_name = 'some-new-node'
        row['ingressDefaultNode'] = new_node_name
        vr = await client.upsert(VantiqResources.K8S_CLUSTERS, row)
        assert isinstance(vr, VantiqResponse)
        assert vr.is_success
        res = vr.body
        print('K8sCluster upsert result:', res)
        assert res is not None
        assert isinstance(res, dict)
        assert 'ingressDefaultNode' in res
        assert res['ingressDefaultNode'] == new_node_name
        # These will be missing since not updated
        assert 'name' not in res
        assert '_id' not in res

        # Fetch to ensure still there & to refresh our record to avoid occ issues
        vr = await client.select_one(VantiqResources.K8S_CLUSTERS, test_cluster_name)
        assert isinstance(vr, VantiqResponse)
        assert vr.is_success
        row = vr.body
        assert row['ingressDefaultNode'] == new_node_name

        # Now, try the same thing via an update
        new_node_name = 'some-other-new-node'
        row['ingressDefaultNode'] = new_node_name
        vr = await client.update(VantiqResources.K8S_CLUSTERS, test_cluster_name, row)
        self.dump_errors('update results: ', vr)
        assert isinstance(vr, VantiqResponse)
        assert vr.is_success
        res = vr.body
        print('K8sCluster update result:', res)

        assert res is not None
        assert isinstance(res, dict)
        assert 'ingressDefaultNode' in res
        assert res['ingressDefaultNode'] == new_node_name
        # These will be missing since not updated
        assert 'name' not in res
        assert '_id' not in res

        # Run a delete operation of something not there -- want to ensure that we're passing the values as required
        vr = await client.delete(VantiqResources.K8S_CLUSTERS, {'name': 'ratherUnlikelyName'})
        assert isinstance(vr, VantiqResponse)
        assert not vr.is_success

        # Now, fetch the updated row
        vr = await client.select_one(VantiqResources.K8S_CLUSTERS, test_cluster_name)
        assert isinstance(vr, VantiqResponse)
        assert vr.is_success
        row = vr.body
        assert row is not None
        assert isinstance(row, dict)
        assert 'name' in row
        assert '_id' in row
        assert 'ingressDefaultNode' in row
        assert row['name'] == test_cluster_name
        assert row['ingressDefaultNode'] == new_node_name
        assert row['_id'] is not None
        vr = await client.delete_one(VantiqResources.K8S_CLUSTERS, test_cluster_name)
        assert isinstance(vr, VantiqResponse)
        assert vr.is_success
        assert vr.count is None

        if not skip_pretest_cleanup:
            vr = await client.delete_one(VantiqResources.K8S_CLUSTERS, 'ratherUnlikelyName')
            assert isinstance(vr, VantiqResponse)
            assert not vr.is_success

        vr = await client.select_one(VantiqResources.K8S_CLUSTERS, 'foo')
        assert isinstance(vr, VantiqResponse)
        assert not vr.is_success
        errs = vr.errors
        assert isinstance(errs, list)
        ve = errs[0]
        assert ve.message == "The requested instance ('[name:foo]') of the k8sclusters resource could not be found."
        assert ve.code == 'io.vantiq.resource.not.found'
        assert ve.params == ['k8sclusters', '[name:foo]']

        vr = await client.select(VantiqResources.K8S_CLUSTERS)
        assert isinstance(vr, VantiqResponse)
        assert vr.is_success
        rows = vr.body
        assert len(rows) == 0

        await client.refresh()

        vr = await client.select('system.junkola')
        assert isinstance(vr, VantiqResponse)
        assert not vr.is_success
        errs = vr.errors
        assert isinstance(errs, list)
        ve = errs[0]
        assert ve.code == 'io.vantiq.type.system.resource.unknown'
        assert ve.message == \
               'The resource junkola is not recognized as a Vantiq system resource.  ' \
               'Either correct resource name or adjust access URI/prefix.'
        assert ve.params == ['junkola']

    async def check_other_operations(self, client: Vantiq):
        vr = await client.count(TEST_TYPE, None)
        assert isinstance(vr, VantiqResponse)
        if not vr.is_success:
            for err in vr.errors:
                print('Error: code: {0}, message: {1}, params: {2}'.format(err.code, err.message, err.params))

        assert vr.is_success
        assert vr.count is not None
        old_count = vr.count
        if vr.count > 0:
            vr = await client.delete(TEST_TYPE, None)
            assert isinstance(vr, VantiqResponse)
            assert vr.is_success
            assert vr.count is not None
            assert vr.count == old_count

        now = datetime.now()
        dt = now.strftime('%Y-%m-%dT%H:%M:%SZ')

        assert isinstance(dt, str)
        embedded = {'a': 1, 'b': 2}
        id_val = 'id_' + str(datetime.now())
        message = {'id': id_val, 'ts': dt,
                   'x': 3.14159, 'k': 8675309, 'o': embedded}

        vr = await client.publish(VantiqResources.TOPICS, TEST_TOPIC, message)
        assert isinstance(vr, VantiqResponse)
        assert vr.is_success

        await asyncio.sleep(0.500)  # let event hit...

        vr = await client.count(TEST_TYPE, None)
        assert isinstance(vr, VantiqResponse)
        if not vr.is_success:
            for err in vr.errors:
                print('Error: code: {0}, message: {1}, params: {2}'.format(err.code, err.message, err.params))
        self.dump_errors('Count error', vr)
        assert vr.is_success
        assert vr.count is not None
        assert vr.count == 1

        # Now verify that the correct object was inserted

        vr = client.select(TEST_TYPE, None, {'id': id_val})
        vr = await vr
        assert isinstance(vr, VantiqResponse)
        assert vr.is_success
        assert isinstance(vr.body, list)
        assert vr.count is None
        for k, v in message.items():
            assert vr.body[0][k] == message[k]

        # Now, verify that we can get the count when desired
        vr = client.select(TEST_TYPE, None, {'id': id_val}, None, 100)
        vr = await vr
        assert isinstance(vr, VantiqResponse)
        assert vr.is_success
        assert isinstance(vr.body, list)
        assert vr.count is not None
        assert vr.count == 1
        for k, v in message.items():
            assert vr.body[0][k] == message[k]

        proc_args = {'arg1': 'I am argument 1', 'arg2': 'I am argument 2'}
        vr = await client.execute(TEST_PROCEDURE, proc_args)
        assert isinstance(vr, VantiqResponse)

        self.dump_errors('Execute error', vr)
        assert vr.is_success
        assert vr.content_type == 'application/json'
        assert vr.body is not None
        assert isinstance(vr.body, dict)
        assert 'arg1' in vr.body
        assert 'arg2' in vr.body
        assert vr.body['arg1'] == proc_args['arg1']
        assert vr.body['arg2'] == proc_args['arg2']

    @staticmethod
    def check_test_conditions():
        if _server_url is None or _access_token is None or (_username is None and _password is None):
            pytest.skip('Need access to Vantiq server.')

    async def check_nsusers_ops(self, client: Vantiq):
        proc_args = {'arg1': 'a1', 'arg2': 'a2'}
        vr = await client.execute(TEST_PROCEDURE, proc_args)
        assert isinstance(vr, VantiqResponse)

        self.dump_errors('Execute error', vr)
        assert vr.is_success
        assert vr.content_type == 'application/json'
        assert vr.body is not None
        assert isinstance(vr.body, dict)
        print('body:', vr.body)
        assert 'arg1' in vr.body
        assert 'arg2' in vr.body
        assert vr.body['arg1'] == proc_args['arg1']
        assert vr.body['arg2'] == proc_args['arg2']
        assert 'namespace' in vr.body
        ns = vr.body['namespace']
        assert isinstance(ns, str)
        vr = await client.get_namespace_users(ns)
        assert isinstance(vr, VantiqResponse)
        self.dump_errors('get_namespace_users error', vr)
        assert vr.is_success
        body = vr.body
        assert isinstance(body, list)
        user_rec = None
        for urec in body:
            pref_name = urec['username']
            if pref_name == _username:
                user_rec = urec
        if user_rec is None:
            # If there's going to be an error, dump some diagnostics
            print('Users: ', body)
        assert user_rec is not None

    @pytest.mark.timeout(10)
    @pytest.mark.asyncio
    async def test_authentication_upw(self):
        global _server_url
        global _username
        global _password

        self.check_test_conditions()

        v = Vantiq(_server_url, '1')
        await v.connect()
        await v.authenticate(_username, _password)
        assert v.is_authenticated()
        assert v.get_id_token() is not None
        assert v.get_access_token() is not None
        assert v.get_username() is not None
        assert v.get_username() == _username

        await v.refresh()
        assert v.is_authenticated()
        assert v.get_id_token() is not None
        assert v.get_access_token() is not None
        assert v.get_username() is not None
        assert v.get_username() == _username

        await v.close()

        # Check that we've dumped connection information
        assert not v.is_authenticated()
        assert v.get_id_token() is None
        assert v.get_access_token() is None
        assert v.get_username() is None

    @pytest.mark.timeout(10)
    @pytest.mark.asyncio
    async def test_authentication_accesstoken(self):
        global _server_url
        global _access_token

        self.check_test_conditions()

        v = Vantiq(_server_url, '1')
        await v.connect()
        await v.set_access_token(_access_token)
        v.set_username(_username)

        assert v.is_authenticated()
        assert v.get_id_token() is None  # In this case, we haven't really talked to the server yet, so no id token.
        assert v.get_access_token() is not None
        assert v.get_username() is not None
        assert v.get_username() == _username

        await v.refresh()
        assert v.is_authenticated()
        assert v.get_id_token() is not None
        assert v.get_access_token() is not None
        await v.close()

        # Check that we've dumped connection information
        assert not v.is_authenticated()
        assert v.get_id_token() is None
        assert v.get_access_token() is None
        assert v.get_username() is None

    @pytest.mark.timeout(20)
    def test_dump_configuration_diagnosis(self):
        print('Config:', _server_url, _access_token, _username, _password)

    @pytest.mark.asyncio
    @pytest.mark.timeout(20)
    async def test_crud_with_ctm(self):
        self.check_test_conditions()
        async with Vantiq(_server_url, '1') as client:
            if _username is not None:
                await client.authenticate(_username, _password)
            else:
                await client.set_access_token(_access_token)

            print('Setup test environment')
            await self.setup_test_env(client)
            print('Begin Context Manager-based CRUD tests')
            await self.check_crud_operations(client, False)

    @pytest.mark.asyncio
    @pytest.mark.timeout(20)
    async def test_crud_with_plain_client(self):
        self.check_test_conditions()
        client = Vantiq(_server_url)  # Also test defaulting of API version
        if _access_token is not None:
            await client.set_access_token(_access_token)
        else:
            await client.authenticate(_username, _password)
        print('Begin non-Context Manager-based CRUD tests')
        await self.check_crud_operations(client, False)
        await client.close()

    @pytest.mark.asyncio
    @pytest.mark.timeout(20)
    async def test_other_ops_with_ctm(self):
        self.check_test_conditions()
        async with Vantiq(_server_url, '1') as client:
            if _username is not None:
                await client.authenticate(_username, _password)
            else:
                await client.set_access_token(_access_token)

            print('Setup test environment')
            await self.setup_test_env(client)
            print('Begin Context Manager-based CRUD tests')
            await self.check_other_operations(client)

    @pytest.mark.asyncio
    @pytest.mark.timeout(20)
    async def test_other_ops_with_plain_client(self):
        self.check_test_conditions()
        client = Vantiq(_server_url)  # Also test defaulting of API version
        if _access_token is not None:
            await client.set_access_token(_access_token)
        else:
            await client.authenticate(_username, _password)
        print('Begin non-Context Manager-based CRUD tests')
        await self.check_other_operations(client)
        await client.close()

    @pytest.mark.asyncio
    @pytest.mark.timeout(20)
    async def test_documentesque_operation_as_ctm(self):
        self.check_test_conditions()

        # Check with CTM style
        async with Vantiq(_server_url, '1') as client:
            await client.set_access_token(_access_token)
            await self.check_documentesque_operation(client)

    @pytest.mark.asyncio
    @pytest.mark.timeout(20)
    async def test_documentesque_operation_as_plain_client(self):
        self.check_test_conditions()

        client = Vantiq(_server_url)
        await client.authenticate(_username, _password)
        await self.check_documentesque_operation(client, False)
        await client.close()

    @pytest.mark.asyncio
    @pytest.mark.timeout(20)
    async def test_subscriptions_as_ctm(self):
        self.check_test_conditions()
        async with Vantiq(_server_url, '1') as client:
            await client.authenticate(_username, _password)
            await self.check_subscription_ops(client, False)

    @pytest.mark.asyncio
    @pytest.mark.timeout(20)
    async def test_subscriptions_as_plain_client(self):
        self.check_test_conditions()
        client = Vantiq(_server_url, '1')
        await client.authenticate(_username, _password)
        await self.check_subscription_ops(client, False)
        await client.close()

    @pytest.mark.asyncio
    @pytest.mark.timeout(0)
    async def test_subscriptions_as_ctm_prestart(self):
        self.check_test_conditions()
        async with Vantiq(_server_url, '1') as client:
            await client.authenticate(_username, _password)
            await self.check_subscription_ops(client, True)

    @pytest.mark.asyncio
    @pytest.mark.timeout(0)
    async def test_subscriptions_as_plain_client_prestart(self):
        self.check_test_conditions()
        client = Vantiq(_server_url, '1')
        await client.authenticate(_username, _password)
        await self.check_subscription_ops(client, True)
        await client.close()

    @pytest.mark.asyncio
    @pytest.mark.timeout(20)
    async def test_namespace_users_as_ctm(self):
        self.check_test_conditions()
        async with Vantiq(_server_url, '1') as client:
            await client.authenticate(_username, _password)
            await self.check_nsusers_ops(client)

    @pytest.mark.asyncio
    @pytest.mark.timeout(20)
    async def test_namespace_users_as_plain_client(self):
        self.check_test_conditions()
        client = Vantiq(_server_url, '1')
        await client.authenticate(_username, _password)
        await self.check_nsusers_ops(client)
        await client.close()
