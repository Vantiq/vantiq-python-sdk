import asyncio
import json

import pytest
from yarl import URL
from typing import Union

from vantiqsdk import Vantiq, VantiqException, VantiqResources, VantiqResponse
import traceback
from aioresponses import aioresponses, CallbackResult
import aiofiles
from datetime import datetime
import urllib.parse

_server_url: str = 'http://example.com/'
_access_token: str = '1234accessToken'
_username: str = 'someuser'
_password: str = 'trulySecret'

TEST_TOPIC = '/test/pythonsdk/topic'
TEST_PROCEDURE = 'pythonsdk.echo'
TEST_TYPE = 'TestType'

STANDARD_COUNT_PROPS = '["_id"]'


class TestMockedConnection:

    @pytest.fixture(autouse=True)
    def _setup(self):
        global _server_url
        """This method replaces/augments the usual __init__(self).  __init__(self) is not supported by pytest.
        Its primary purpose here is to 'declare' (via assignment) the instance variables.
        """
        self._acquired_doc = None
        self._doc_is_from = None
        self.callback_count = 0
        self.callbacks = []
        self.message_checker = None
        self.server_url = _server_url
        self.base_url = self.server_url + '/api/v1/'

    def dump_errors(self, tag: str, vr: VantiqResponse):
        if not vr.is_success:
            for err in vr.errors:
                print('{0}: {1}'.format(tag, err))

    def process_chunk(self, doc_url: str, length: int, chunk: bytes) -> None:
        assert length > 0
        assert len(chunk) == length
        if self._doc_is_from is None:
            self._doc_is_from = doc_url
            assert len(self._acquired_doc) == 0  # Failure here is probably a test issue rather than product...
        else:
            assert self._doc_is_from == doc_url
        self._acquired_doc.extend(chunk)

    async def check_download(self, mocked, client: Vantiq, content_url: str, expected_content: Union[bytes, bytearray]):
        # Check download direct

        mocked.get(content_url, status=200, body=expected_content)
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
            assert msg['body']['value'] == {'testValue': 'topic value'}

    async def subscriber_callback(self, what: str, details: dict) -> None:
        print('Subscriber got a callback -- what: {0}, details: {1}'.format(what, details))
        self.callback_count += 1
        self.callbacks.append(what)
        if self.message_checker is not None:
            self.message_checker(what, details)

    async def callback(self, url: URL, **kwargs):
        if TEST_TOPIC in url.path:
            await self.subscriber_callback('connect', {})
            await self.subscriber_callback('message', {'status': 200,
                                                       'body': {'path': '/topics/test/pythonsdk/topic/publish',
                                                                'value': {'testValue': 'topic value'}}})
        elif TEST_TYPE in url.path:
            await self.subscriber_callback('connect', {})
            await self.subscriber_callback('message', {'status': 200,
                                                       'body': {'path': '/types/TestType/insert',
                                                                'value': {'id': 'test_insert'}}})
        else:
            pytest.xfail('Unexpected path entry in callback generator')
        return CallbackResult(status=200)

    async def check_subscription_ops(self, mocked, client: Vantiq):
        # TODO: if/when aioresponses() handles websocket mocking, do that
        # In the mean time, we'll simulate the event delivery using the aioresponses()
        # callback functions
        # Leaving this code in, for now, hoping that the TODO above is fulfilled
        # task = client.make_sub_connection()
        # asyncio.create_task(task)

        query_part = self.mock_query_part(where_part='{}')
        mocked.delete(f'/api/v1/resources/custom/{TEST_TYPE}?count=true&{query_part}', status=200)

        # assert client._subscriber is not None
        # if client._subscriber is not None:
        #     client._subscriber.connected = True
        # while client._subscriber is None or not client._subscriber.connected:
        #     await asyncio.sleep(0.1)
        # vr = await client.subscribe(VantiqResources.TOPICS, TEST_TOPIC, None, self.subscriber_callback, {})
        # assert isinstance(vr, VantiqResponse)
        # self.dump_errors('Subscription Error', vr)
        # assert vr.is_success
        # await asyncio.sleep(0.5)

        self.message_checker = self.check_callback_topic_publish

        orig_count = 0

        mocked.post(f'/api/v1/resources/topics/{TEST_TOPIC}', status=200,
                    callback=self.callback)
        vr = await client.publish(VantiqResources.TOPICS, TEST_TOPIC, {'testValue': 'topic value'})
        assert isinstance(vr, VantiqResponse)
        assert vr.is_success

        # Now, we should see that our callback was called after a little while.
        # Should be immediate, but this is harmless

        while self.callback_count < orig_count + 2:
            await asyncio.sleep(0.1)
        assert self.callbacks == ['connect', 'message']

        self.callbacks = []

        # vr = await client.subscribe(VantiqResources.TYPES, TEST_TYPE, 'insert', self.subscriber_callback, {})
        # assert isinstance(vr, VantiqResponse)
        # self.dump_errors('Subscription Error', vr)
        # assert vr.is_success
        # await asyncio.sleep(0.5)

        orig_count = self.callback_count
        self.message_checker = self.check_callback_type_insert

        mocked.post(f'/api/v1/resources/custom/{TEST_TYPE}', status=200,
                    callback=self.callback)

        vr = await client.insert(TEST_TYPE, {'id': 'test_insert'})
        assert isinstance(vr, VantiqResponse)
        assert vr.is_success

        # Now, we should see that our callback was called after a little while.

        while self.callback_count < orig_count + 2:
            await asyncio.sleep(0.1)
        assert self.callbacks == ['connect', 'message']

    async def check_documentesque_operation(self, mocked, client: Vantiq, skip_pretest_cleanup: bool = False) -> None:
        # First, clean the environment
        all_docs = []
        mocked.delete('/api/v1/resources/documents?count=true', status=200)
        if not skip_pretest_cleanup:
            await client.delete(VantiqResources.DOCUMENTS, None)

        # Check doc where contents are directly inserted
        file_content = 'abcdefgh' * 1000
        doc = {'name': 'test_doc', 'fileType': 'text/plain', 'content': file_content}
        doc_resp = {'name': 'test_doc', 'contentSize': len(file_content), 'fileType': 'text/plain',
                    'content': '/docs/test_doc'}
        all_docs.append(doc_resp)

        mocked.post('/api/v1/resources/documents', status=200, payload=doc_resp)
        vr = await client.insert(VantiqResources.DOCUMENTS, doc)
        assert isinstance(vr, VantiqResponse)
        assert vr.is_success
        res = vr.body
        assert res is not None
        assert isinstance(res, dict)
        assert res['name'] == 'test_doc'
        mocked.get('/api/v1/resources/documents/test_doc', status=200, payload=doc_resp)
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
        await self.check_download(mocked, client, doc['content'], bytes(file_content, 'utf-8'))

        # Now, test ability to upload a document (as opposed to insert as above)
        # Check for creation as well as content existence & fetchability
        file_content = '1234567890' * 2000
        file_name = 'test_doc_upload'
        doc_resp = {'name': file_name, 'contentSize': len(file_content), 'fileType': 'text/plain',
                    'content': '/docs/' + file_name}
        all_docs.append(doc_resp)
        mocked.post('/api/v1/resources/documents', status=200, headers={'contentType': 'application/json'},
                    payload=doc_resp)

        vr = await client.upload(VantiqResources.DOCUMENTS, 'text/plain',
                                 filename=file_name, inmem=file_content)
        assert isinstance(vr, VantiqResponse)
        assert vr.is_success
        res = vr.body
        assert res is not None
        assert isinstance(res, dict)
        print('Document uploaded: ', res)
        mocked.get('/api/v1/resources/documents/test_doc_upload', status=200, payload=doc_resp)
        vr = await client.select_one(VantiqResources.DOCUMENTS, 'test_doc_upload')
        print('Document fetch post insert', vr)
        assert isinstance(vr, VantiqResponse)
        assert vr.is_success
        doc = vr.body
        assert doc['contentSize'] == len(file_content)

        await self.check_download(mocked, client, doc['content'], bytes(file_content, 'utf-8'))

        # Now, check the same thing with a file located at a root location
        # This is of interest since Vantiq doesn't permit document names that start
        # with '/'.  This also checks to ensure that path names work as document names
        # (as they should), but the mechanism used can url-encode when not expected.
        filename = '/tmp/test_file'
        filename_sans_leading_slash = filename[len('/'):]  # .removeprefix('/')
        file_content = 'ZYXWVUTSRQPONMLKJIHGFEDCBA' * 250
        async with aiofiles.open(filename, mode='wb') as f:
            await f.write(bytes(file_content, 'utf-8'))

        doc_resp = {'name': filename_sans_leading_slash, 'contentSize': len(file_content), 'fileType': 'text/plain',
                    'content': '/docs/' + filename_sans_leading_slash}
        all_docs.append(doc_resp)
        mocked.post('/api/v1/resources/documents', status=200, headers={'contentType': 'application/json'},
                    payload=doc_resp)

        vr = await client.upload(VantiqResources.DOCUMENTS, 'text/plain', filename=filename,
                                 doc_name=filename_sans_leading_slash)
        assert isinstance(vr, VantiqResponse)
        assert vr.is_success
        doc = vr.body
        assert isinstance(doc, dict)
        # Verify document set is as we expect
        mocked.get('/api/v1/resources/documents', status=200, payload=all_docs)

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

        qp = self.mock_query_part(props_part=STANDARD_COUNT_PROPS)
        url = f'/api/v1/resources/documents?count=true&limit=1&{qp}'
        mocked.get(url, status=200, headers={'X-Total-Count': '3'})
        vr = await client.count(VantiqResources.DOCUMENTS, None)
        assert vr.count is not None
        assert vr.count == len(docs)

        mocked.get('/api/v1/resources/documents/' + filename_sans_leading_slash, status=200, payload=doc_resp)
        vr = await client.select_one(VantiqResources.DOCUMENTS, filename_sans_leading_slash)
        assert isinstance(vr, VantiqResponse)
        assert vr.is_success
        doc = vr.body
        assert isinstance(doc, dict)
        assert doc['name'] == filename_sans_leading_slash
        assert doc['contentSize'] == len(file_content)

        await self.check_download(mocked, client, doc['content'], bytes(file_content, 'utf-8'))

    def mock_query_part(self, props_part: str = None, where_part: str = None, sort_part: str = None,
                        limit_part: int = None, option_part: dict = None) -> str:
        encoded_props = urllib.parse.quote(props_part) if props_part is not None else ''
        encoded_where = urllib.parse.quote(where_part) if where_part is not None else ''
        encoded_sort = urllib.parse.quote(sort_part) if sort_part is not None else ''
        # TODO -- when aioresponses() fixes double encoding problem..., remove the re-encoding below...
        encoded_props = urllib.parse.quote(encoded_props) if props_part is not None else ''
        encoded_where = urllib.parse.quote(encoded_where) if where_part is not None else ''
        encoded_sort = urllib.parse.quote(encoded_sort) if sort_part is not None else ''
        # TODO -- end double encoding hack.
        ret_val = ''
        if limit_part is not None:
            ret_val += f'count=true&limit={limit_part}'
        if props_part is not None:
            if ret_val != '':
                ret_val += '&'
            ret_val += f'props={encoded_props}'
        if option_part is not None:
            for k, v in option_part.items():
                if ret_val != '':
                    ret_val += '&'
                ret_val += f'{k}={v}'
        if sort_part is not None:
            if ret_val != '':
                ret_val += '&'
            ret_val += f'sort={encoded_sort}'
        if where_part is not None:
            if ret_val != '':
                ret_val += '&'
            ret_val += f'where={encoded_where}'
        return ret_val

    async def check_crud_operations(self, mocked, client: Vantiq, skip_pretest_cleanup: bool = False):
        mocked.get('/api/v1/resources/types', status=200, headers={'contentType': 'application/json'},
                   body=json.dumps([{'name': 'Ars_Type', 'resourceName': 'types', 'ars_namespace': 'system'},
                                    {'name': 'Ars_K8sCluster', 'resourceName': 'system.k8sclusters',
                                     'ars_namespace': 'system'}]))
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
        mocked.get('/api/v1/resources/types/ArsNamespace', status=200, headers={'contentType': 'application/json'},
                   body=json.dumps({'name': 'Ars_Namespace', 'resourceName': 'namespaces', 'ars_namespace': 'system'}))

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
            query_part =\
                self.mock_query_part(props_part='["name", "resourceName"]',
                                     where_part='{"$or": [{"name": "ArsType"}, {"name": "ArsTensorFlowModel"}]}',
                                     sort_part='{"name": -1}', limit_part=1,
                                     option_part={"required": "true"})
            mocked.get(url=f'/api/v1/resources/types?{query_part}', status=200,
                       headers={'contentType': 'application/json'},
                       body=json.dumps([{'name': 'ArsType', 'resourceName': 'types'}]))
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

            mocked.get('/api/v1/resources/types', status=200, headers={'contentType': 'application/json'},
                       body=json.dumps([{'name': 'Ars_Type', 'resourceName': 'types', 'ars_namespace': 'system'},
                                        {'name': 'Ars_K8sCluster', 'resourceName': 'system.k8sclusters',
                                         'ars_namespace': 'system'}]))
            vr = await client.select(VantiqResources.TYPES)
            assert isinstance(vr, VantiqResponse)
            assert vr.is_success
            rows = vr.body
            assert rows is not None
            assert isinstance(rows, list)
            assert len(rows) > 0
            qp = self.mock_query_part(props_part=STANDARD_COUNT_PROPS)
            mocked.get(f'/api/v1/resources/types?count=true&limit=1&{qp}', status=200,
                       headers={'X-Total-Count': '2', 'contentType': 'application/json'},
                       body=json.dumps([{'name': 'Ars_Type', 'resourceName': 'types', 'ars_namespace': 'system'},
                                        {'name': 'Ars_K8sCluster', 'resourceName': 'system.k8sclusters',
                                         'ars_namespace': 'system'}]))

            vr = await client.count(VantiqResources.TYPES, None)
            assert vr.is_success
            assert vr.count is not None
            assert vr.count == len(rows)

            query_part = self.mock_query_part(where_part='{"resourceName": "types"}', props_part=STANDARD_COUNT_PROPS)
            mocked.get(f'/api/v1/resources/types?count=true&limit=1&{query_part}', status=200,
                       headers={'X-Total-Count': '1', 'contentType': 'application/json'},
                       body=json.dumps([{'name': 'Ars_Type', 'resourceName': 'types', 'ars_namespace': 'system'}]))

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

        test_cluster_name = 'pythonTestCluster'
        mocked.post('/api/v1/resources/k8sclusters', status=200, headers={'contentType': 'application/json'},
                    body=json.dumps({'_id': 1234, 'name': test_cluster_name,
                                     'ingressDefaultNode': f'vantiq-{test_cluster_name}-node'.lower()}))
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

        mocked.get(f'/api/v1/resources/k8sclusters/{test_cluster_name}', status=200,
                   headers={'contentType': 'application/json'},
                   body=json.dumps({'_id': '1234', 'name': test_cluster_name,
                                    'ingressDefaultNode': f'vantiq-{test_cluster_name}-node'.lower()}))
        vr = await client.select_one(VantiqResources.K8S_CLUSTERS, test_cluster_name)
        assert isinstance(vr, VantiqResponse)
        assert vr.is_success
        row = vr.body
        assert row is not None
        assert isinstance(row, dict)

        new_node_name = 'some-new-node'
        row['ingressDefaultNode'] = new_node_name
        mocked.post(f'/api/v1/resources/k8sclusters?upsert=true', status=200,
                    body=json.dumps({'ingressDefaultNode': f'{new_node_name}'.lower()}))

        vr = await client.upsert(VantiqResources.K8S_CLUSTERS, row)
        assert isinstance(vr, VantiqResponse)
        assert vr.is_success
        res = vr.body
        assert res is not None
        assert isinstance(res, dict)
        assert 'ingressDefaultNode' in res
        assert res['ingressDefaultNode'] == new_node_name
        # These will be missing since not updated
        assert 'name' not in res
        assert '_id' not in res

        # Fetch to ensure still there & to refresh our record to avoid occ issues
        mocked.get(f'/api/v1/resources/k8sclusters/{test_cluster_name}', status=200,
                   headers={'contentType': 'application/json'},
                   body=json.dumps({'_id': '1234', 'name': test_cluster_name,
                                    'ingressDefaultNode': f'{new_node_name}'.lower()}))
        vr = await client.select_one(VantiqResources.K8S_CLUSTERS, test_cluster_name)
        assert isinstance(vr, VantiqResponse)
        assert vr.is_success
        row = vr.body
        assert row['ingressDefaultNode'] == new_node_name

        # Now, try the same thing via an update
        new_node_name = 'some-other-new-node'
        row['ingressDefaultNode'] = new_node_name
        mocked.put(f'/api/v1/resources/k8sclusters/{test_cluster_name}', status=200,
                   body=json.dumps({'ingressDefaultNode': f'{new_node_name}'.lower()}))

        vr = await client.update(VantiqResources.K8S_CLUSTERS, test_cluster_name, row)
        self.dump_errors('update results: ', vr)
        assert isinstance(vr, VantiqResponse)
        assert vr.is_success
        res = vr.body
        print('K8sCluster update result:', res)

        query_part = self.mock_query_part(where_part='{"name": "ratherUnlikelyName"}')
        url = f'/api/v1/resources/k8sclusters?count=true&{query_part}'
        mocked.delete(url, status=404)
        # Run delete of something not there -- want to ensure that we're passing the values as required
        vr = await client.delete(VantiqResources.K8S_CLUSTERS, {'name': 'ratherUnlikelyName'})
        assert isinstance(vr, VantiqResponse)
        assert not vr.is_success

        mocked.get(f'/api/v1/resources/k8sclusters/{test_cluster_name}', status=200,
                   body=json.dumps({'_id': '1234', 'name': test_cluster_name,
                                    'ingressDefaultNode': f'{new_node_name}'.lower()}))

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
        mocked.delete(f'/api/v1/resources/k8sclusters/{test_cluster_name}', status=200)
        vr = await client.delete_one(VantiqResources.K8S_CLUSTERS, test_cluster_name)
        assert isinstance(vr, VantiqResponse)
        assert vr.is_success
        assert vr.count is None

        if not skip_pretest_cleanup:
            mocked.delete(f'/api/v1/resources/k8sclusters/ratherUnlikelyName', status=404)
            vr = await client.delete_one(VantiqResources.K8S_CLUSTERS, 'ratherUnlikelyName')
            assert isinstance(vr, VantiqResponse)
            assert not vr.is_success

        mocked.get(f'/api/v1/resources/k8sclusters/foo', status=404,
                   body=json.dumps({'code': 'io.vantiq.resource.not.found',
                                    'message': "The requested instance ('[name:foo]') of the k8sclusters resource"
                                               " could not be found.",
                                    'params': ['k8sclusters', '[name:foo]']}))
        vr = await client.select_one(VantiqResources.K8S_CLUSTERS, 'foo')
        assert isinstance(vr, VantiqResponse)
        assert not vr.is_success
        errs = vr.errors
        assert isinstance(errs, list)
        ve = errs[0]
        assert ve.message == "The requested instance ('[name:foo]') of the k8sclusters resource could not be found."
        assert ve.code == 'io.vantiq.resource.not.found'
        assert ve.params == ['k8sclusters', '[name:foo]']

        mocked.get('/api/v1/resources/k8sclusters', status=200, body=json.dumps([]))

        vr = await client.select(VantiqResources.K8S_CLUSTERS)
        assert isinstance(vr, VantiqResponse)
        assert vr.is_success
        rows = vr.body
        assert len(rows) == 0

        mocked.post('/authenticate/refresh',
                    status=200,
                    headers={'contentType': 'application/json'},
                    body=json.dumps({'accessToken': '1234abcd', 'idToken': 'longer_token'}))
        await client.refresh()

        mocked.get(f'/api/v1/resources/junkola', status=404,
                   body=json.dumps([{'code': 'io.vantiq.type.system.resource.unknown',
                                     'message': 'The resource junkola is not recognized as a Vantiq system resource.  '
                                                'Either correct resource name or adjust access URI/prefix.',
                                     'params': ['junkola']}]))

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

    async def check_other_operations(self, mocked, client: Vantiq):
        qp = self.mock_query_part(props_part=STANDARD_COUNT_PROPS)
        mocked.get(f'/api/v1/resources/custom/{TEST_TYPE}?count=true&limit=1&{qp}', status=200, headers={'X-Total-Count': '0'})

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
        id_val = 'some_id'
        message = {'id': id_val, 'ts': dt,
                   'x': 3.14159, 'k': 8675309, 'o': embedded}

        mocked.post(f'/api/v1/resources/topics/{TEST_TOPIC}', status=200)
        vr = await client.publish(VantiqResources.TOPICS, TEST_TOPIC, message)
        assert isinstance(vr, VantiqResponse)
        assert vr.is_success

        await asyncio.sleep(0.500)  # let event hit...

        qp = self.mock_query_part(props_part=STANDARD_COUNT_PROPS)
        mocked.get(f'/api/v1/resources/custom/{TEST_TYPE}?count=true&limit=1&{qp}', status=200, headers={'X-Total-Count': '1'})
        vr = await client.count(TEST_TYPE, None)
        assert isinstance(vr, VantiqResponse)
        self.dump_errors('Count error', vr)
        assert vr.is_success
        assert vr.count is not None
        assert vr.count == 1

        # Now verify that the correct object was inserted
        query_part = self.mock_query_part(where_part='{"id": "some_id"}')
        url = f'/api/v1/resources/custom/{TEST_TYPE}?{query_part}'
        mocked.get(url, status=200, body=json.dumps([message]))
        vr = client.select(TEST_TYPE, None, {'id': id_val})
        vr = await vr
        assert isinstance(vr, VantiqResponse)
        assert vr.is_success
        assert isinstance(vr.body, list)
        assert vr.count is None
        for k, v in message.items():
            assert vr.body[0][k] == message[k]

        # Now verify that the correct object was inserted
        query_part = self.mock_query_part(where_part='{"id": "some_id"}', limit_part=100)

        url = f'/api/v1/resources/custom/{TEST_TYPE}?{query_part}'

        # Now, verify that we can get the count when desired
        mocked.get(url, status=200, headers={'X-Total-Count': '1', 'contentType': 'application/json'},
                   body=json.dumps([message]))
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
        ret_val = proc_args.copy()
        ret_val['namespace'] = 'bogusTestNamespace'
        mocked.post(f'/api/v1/resources/procedures/{TEST_PROCEDURE}',
                    status=200,
                    headers={'contentType': 'application/json'},
                    body=json.dumps(ret_val))

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

    async def check_nsusers_ops(self, mocked, client: Vantiq):
        global _username
        proc_args = {'arg1': 'a1', 'arg2': 'a2'}
        ret_val = proc_args.copy()
        ret_val['namespace'] = 'bogusTestNamespace'
        mocked.post(f'/api/v1/resources/procedures/{TEST_PROCEDURE}',
                    status=200,
                    headers={'contentType': 'application/json'},
                    body=json.dumps(ret_val))
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
        assert 'namespace' in vr.body
        ns = vr.body['namespace']
        assert isinstance(ns, str)
        expected_result = [{'username': _username, 'preferredUsername': _username}]
        mocked.get(f'/api/v1/resources/namespaces/{ns}/authorizedUsers',
                   status=200,
                   headers={'contentType': 'application/json'},
                   body=json.dumps(expected_result))
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

        with aioresponses() as mocked:
            v = Vantiq(_server_url, '1')
            await v.connect()
            mocked.get('/authenticate',
                       status=200,
                       headers={'contentType': 'application/json'},
                       body=json.dumps({'accessToken': '1234abcd', 'idToken': 'longer_token'}))
            await v.authenticate(_username, _password)
            assert v.is_authenticated()
            assert v.get_id_token() is not None
            assert v.get_access_token() is not None
            assert v.get_username() is not None
            assert v.get_username() == _username

            mocked.post('/authenticate/refresh',
                        status=200,
                        headers={'contentType': 'application/json'},
                        body=json.dumps({'accessToken': '1234abcd', 'idToken': 'longer_token'}))
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
        with aioresponses() as mocked:
            v = Vantiq(_server_url, '1')
            await v.connect()
            await v.set_access_token(_access_token)
            v.set_username(_username)

            assert v.is_authenticated()
            assert v.get_id_token() is None  # In this case, we haven't really talked to the server yet, so no id token.
            assert v.get_access_token() is not None
            assert v.get_username() is not None
            assert v.get_username() == _username

            mocked.post('/authenticate/refresh',
                        status=200,
                        headers={'contentType': 'application/json'},
                        body=json.dumps({'accessToken': '1234abcd', 'idToken': 'longer_token'}))
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

    @pytest.mark.asyncio
    @pytest.mark.timeout(20)
    async def test_crud_with_ctm(self):
        with aioresponses() as mocked:
            async with Vantiq(_server_url, '1') as client:
                if _username is not None:
                    mocked.get('/authenticate',
                               status=200,
                               headers={'contentType': 'application/json'},
                               body=json.dumps({'accessToken': '1234abcd', 'idToken': 'longer_token'}))
                    await client.authenticate(_username, _password)
                else:
                    await client.set_access_token(_access_token)

                print('Begin Context Manager-based CRUD tests')
                await self.check_crud_operations(mocked, client, False)

    @pytest.mark.asyncio
    @pytest.mark.timeout(20)
    async def test_crud_with_plain_client(self):
        with aioresponses() as mocked:
            client = Vantiq(_server_url)  # Also test defaulting of API version
            if _access_token is not None:
                await client.set_access_token(_access_token)
            else:
                mocked.get('/authenticate',
                           status=200,
                           headers={'contentType': 'application/json'},
                           body=json.dumps({'accessToken': '1234abcd', 'idToken': 'longer_token'}))
                await client.authenticate(_username, _password)
            print('Begin non-Context Manager-based CRUD tests')
            await self.check_crud_operations(mocked, client, False)
            await client.close()

    @pytest.mark.asyncio
    @pytest.mark.timeout(20)
    async def test_other_ops_with_ctm(self):
        with aioresponses() as mocked:
            mocked.get('/authenticate',
                       status=200,
                       headers={'contentType': 'application/json'},
                       body=json.dumps({'accessToken': '1234abcd', 'idToken': 'longer_token'}))
            async with Vantiq(_server_url, '1') as client:
                if _username is not None:
                    await client.authenticate(_username, _password)
                else:
                    await client.set_access_token(_access_token)

                print('Begin Context Manager-based CRUD tests')
                await self.check_other_operations(mocked, client)

    @pytest.mark.asyncio
    @pytest.mark.timeout(20)
    async def test_other_ops_with_plain_client(self):
        with aioresponses() as mocked:
            mocked.get('/authenticate',
                       status=200,
                       headers={'contentType': 'application/json'},
                       body=json.dumps({'accessToken': '1234abcd', 'idToken': 'longer_token'}))
            client = Vantiq(_server_url)  # Also test defaulting of API version
            if _access_token is not None:
                await client.set_access_token(_access_token)
            else:
                await client.authenticate(_username, _password)
            print('Begin non-Context Manager-based CRUD tests')
            await self.check_other_operations(mocked, client)
            await client.close()

    @pytest.mark.asyncio
    @pytest.mark.timeout(20)
    async def test_documentesque_operation_as_ctm(self):
        # Check with CTM style
        async with Vantiq(_server_url, '1') as client:
            with aioresponses() as mocked:
                await client.set_access_token(_access_token)
                await self.check_documentesque_operation(mocked, client)

    @pytest.mark.asyncio
    @pytest.mark.timeout(20)
    async def test_documentesque_operation_as_plain_client(self):
        client = Vantiq(self.server_url)
        with aioresponses() as mocked:
            mocked.get('/authenticate',
                       status=200,
                       headers={'contentType': 'application/json'},
                       body=json.dumps({'accessToken': '1234abcd', 'idToken': 'longer_token'}))
            await client.authenticate(_username, _password)
            await self.check_documentesque_operation(mocked, client, False)
            await client.close()

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_subscriptions_as_ctm(self):
        # self.check_test_conditions()
        with aioresponses() as mocked:
            async with Vantiq(_server_url, '1') as client:
                mocked.get('/authenticate',
                           status=200,
                           headers={'contentType': 'application/json'},
                           body=json.dumps({'accessToken': '1234abcd', 'idToken': 'longer_token'}))
                await client.authenticate(_username, _password)
                await self.check_subscription_ops(mocked, client)

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_subscriptions_as_plain_client(self):
        with aioresponses() as mocked:
            client = Vantiq(_server_url, '1')
            mocked.get('/authenticate',
                       status=200,
                       headers={'contentType': 'application/json'},
                       body=json.dumps({'accessToken': '1234abcd', 'idToken': 'longer_token'}))
            await client.authenticate(_username, _password)
            await self.check_subscription_ops(mocked, client)
            await client.close()

    @pytest.mark.asyncio
    @pytest.mark.timeout(0)
    async def test_namespace_users_as_ctm(self):
        async with Vantiq(_server_url, '1') as client:
            with aioresponses() as mocked:
                mocked.get('/authenticate',
                           status=200,
                           headers={'contentType': 'application/json'},
                           body=json.dumps({'accessToken': '1234abcd', 'idToken': 'longer_token'}))
                await client.authenticate(_username, _password)
                await self.check_nsusers_ops(mocked, client)

    @pytest.mark.asyncio
    @pytest.mark.timeout(0)
    async def test_namespace_users_as_plain_client(self):
        client = Vantiq(_server_url, '1')
        with aioresponses() as mocked:
            mocked.get('/authenticate',
                       status=200,
                       headers={'contentType': 'application/json'},
                       body=json.dumps({'accessToken': '1234abcd', 'idToken': 'longer_token'}))
            await client.authenticate(_username, _password)
            await self.check_nsusers_ops(mocked, client)
        await client.close()
